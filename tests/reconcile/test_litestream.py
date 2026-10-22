"""`litestream` — only when a db replicates."""

import os
import tempfile
from pathlib import Path
import unittest

from anci.render import litestream as litestream_render, systemd
from anci import layout
from anci.converge import litestream

from .support import file_at, link_at, load_conf, make_ctx

CONF_REPL = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[exe.app]
source = './app'

[replication]
bucket = 'backups'

[db.main-db]
migrations = './m'

[db.main-db.replication]
"""


class TestLitestream(unittest.TestCase):
    def test_no_replication_contributes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            c = litestream.contribute(make_ctx(conf))
            self.assertEqual(c.files, [])
            self.assertEqual(c.downloads, [])
            self.assertEqual(c.after_apply, [])

    def test_replication_installs_unit_and_yaml(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, CONF_REPL)
            c = litestream.contribute(make_ctx(conf))
            replicated = conf.replicated_dbs("dev")
            self.assertEqual(file_at(c, layout.LITESTREAM_FILE).content,
                             litestream_render.render_litestream_yaml(replicated))
            self.assertEqual(
                file_at(c, f"{layout.UNITS_DIR}/litestream.service").content,
                systemd.render_litestream_unit())
            self.assertEqual(c.downloads, [])   # the binary ships with `vendored`
            self.assertEqual(link_at(c, "/etc/systemd/system/litestream.service").target,
                             f"{layout.UNITS_DIR}/litestream.service")
            self.assertEqual([a.phase for a in c.after_apply], [2])


def _db(name, path=None):
    from anci.conf import Replication
    return type("D", (), {"name": name,
                          "replication": Replication(bucket="b", path=path or f"dev/{name}")})()


class TestReplicaTargetChange(unittest.TestCase):
    """litestream's local state belongs to one replica target; a moved target
    gets a fresh start, run here through a real shell."""

    def test_state_is_reset_only_when_the_target_moved(self):
        import subprocess
        from unittest import mock
        with tempfile.TemporaryDirectory() as td, mock.patch.object(layout, "DB_DIR", td):
            for name, recorded in (("moved", "x b dev/old"), ("same", " b dev/same"), ("new", None)):
                os.makedirs(layout.litestream_state_dir(name))
                if recorded is not None:
                    Path(layout.replica_record(name)).write_text(recorded + "\n")
            dbs = [_db("moved"), _db("same"), _db("new")]
            subprocess.run(["sh", "-c", litestream._reset_moved(dbs)], check=True)
            self.assertFalse(os.path.exists(layout.litestream_state_dir("moved")))
            self.assertTrue(os.path.exists(layout.litestream_state_dir("same")))
            self.assertTrue(os.path.exists(layout.litestream_state_dir("new")))   # no record: kept
            for d in dbs:
                self.assertEqual(Path(layout.replica_record(d.name)).read_text().strip(),
                                 litestream._target(d).strip())


class TestLitestreamActivation(unittest.TestCase):
    """A converge must not restart a healthy litestream unless something it reads changed."""

    def _script(self, *, files=(), links=()):
        from unittest import mock
        from anci import ssh
        from anci.apply import ApplyResult
        sudo = mock.MagicMock()
        unit_path = f"{layout.UNITS_DIR}/litestream.service"
        run = litestream._activate("litestream.service", unit_path,
                                   "/etc/systemd/system/litestream.service",
                                   [_db("main-db")])
        with mock.patch.object(ssh, "sudo", sudo):
            status = run(None, ApplyResult(changed_files=list(files), changed_links=list(links)))
        return status, sudo.call_args.args[1]

    def test_unchanged_only_ensures_it_is_running(self):
        status, script = self._script()
        self.assertEqual(status, "running")
        self.assertNotIn("systemctl restart", script)
        self.assertNotIn("daemon-reload", script)
        self.assertIn("is-active --quiet litestream.service || systemctl start litestream.service", script)
        self.assertIn("systemctl enable litestream.service", script)

    def test_changes_litestream_reads_at_start_restart_it(self):
        for changed in (layout.LITESTREAM_BIN, layout.LITESTREAM_FILE, layout.SECRETS_FILE,
                        f"{layout.UNITS_DIR}/litestream.service"):
            status, script = self._script(files=[changed])
            self.assertEqual(status, "restarted", changed)
            self.assertIn("systemctl stop litestream.service", script, changed)
            self.assertIn("systemctl start litestream.service", script, changed)

    def test_unit_change_reloads_systemd(self):
        _, script = self._script(links=["/etc/systemd/system/litestream.service"])
        self.assertIn("daemon-reload", script)
