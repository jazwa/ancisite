import contextlib
import io
import json
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from anci.render import litestream, systemd
from anci import box
from anci.commands import dbs
from anci import errors, ssh, ui
from anci import secrets as secrets_mod
from anci.conf import Conf, Db, Env, Exe, ExeOverride, Replication, User


# these tests drive whole verbs over a mocked transport; the box lock is a real
# ssh session, so it's a no-op here (tests/test_boxlock.py covers it)
_lock_patch = mock.patch("anci.boxlock.hold", lambda *a, **kw: contextlib.nullcontext())


def setUpModule():
    _lock_patch.start()


def tearDownModule():
    _lock_patch.stop()


def make_conf(tmp: Path) -> Conf:
    mig_dir = tmp / "migrations"
    mig_dir.mkdir(exist_ok=True)
    (mig_dir / "0001_create.sql").write_text("CREATE TABLE notes (id INTEGER PRIMARY KEY);")
    (mig_dir / "0002_add.sql").write_text("ALTER TABLE notes ADD COLUMN body TEXT;")
    return Conf(
        users={"jack": User(name="jack", ssh_pub_keys=["k"])},
        envs={"dev": Env(name="dev", ip="127.0.0.1", access_users=["jack"],
                         anci_version="0.0.1")},
        exes={"app": Exe(
            name="app", source="./bin/app",
            env_overrides={},
        )},
        dbs={"main-db": Db(
            name="main-db", migrations=str(mig_dir),
            extensions=[str(tmp / "ext.so")],
            replication=Replication(bucket="backups"),
        )},
    )


class TestDbEnvVars(unittest.TestCase):
    def test_implicit_injection_for_every_db(self):
        conf = make_conf(Path(tempfile.mkdtemp()))
        vars = box.db_env_vars(conf, "dev")
        self.assertEqual(vars["ANCI_DB_MAIN_DB_PATH"], "/var/lib/anci/db/main-db/data.sql")
        self.assertEqual(vars["ANCI_DB_MAIN_DB_EXT_DIR"], "/var/lib/anci/ext/main-db")


class TestListMigrations(unittest.TestCase):
    def test_glob_order_and_names(self):
        with tempfile.TemporaryDirectory() as td:
            conf = make_conf(Path(td))
            migs = dbs.list_migrations(conf.dbs["main-db"])
            self.assertEqual([n for n, _ in migs], ["0001_create", "0002_add"])

    def test_missing_dir_is_conf_error(self):
        db = Db(name="x", migrations="/nonexistent/dir")
        with self.assertRaises(errors.ConfError):
            dbs.list_migrations(db)


class TestDbBuild(unittest.TestCase):
    """local-schema runs real sqlite on the dev machine."""

    def test_builds_schema_and_bookkeeping(self):
        with tempfile.TemporaryDirectory() as td:
            conf = make_conf(Path(td))
            scratch = dbs.local_schema(conf, "main-db", "dev")
            self.assertTrue(scratch.is_file())
            conn = sqlite3.connect(scratch)
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("notes", tables)
            self.assertIn(dbs.BOOKKEEPING_TABLE, tables)
            count = conn.execute(
                f"SELECT COUNT(*) FROM {dbs.BOOKKEEPING_TABLE}"
            ).fetchone()[0]
            self.assertEqual(count, 2)
            checksummed = conn.execute(
                f"SELECT COUNT(*) FROM {dbs.BOOKKEEPING_TABLE} WHERE checksum IS NOT NULL"
            ).fetchone()[0]
            self.assertEqual(checksummed, 2)
            conn.close()

    def test_last_statement_without_semicolon(self):
        # the sqlite3 CLI (`migrate`) runs it; local-schema must too
        with tempfile.TemporaryDirectory() as td:
            conf = make_conf(Path(td))
            (Path(td) / "migrations" / "0003_index.sql").write_text(
                "CREATE INDEX idx_body ON notes(body)\n")
            scratch = dbs.local_schema(conf, "main-db", "dev")
            conn = sqlite3.connect(scratch)
            self.assertEqual(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_body'").fetchone(),
                ("idx_body",))
            self.assertEqual(
                conn.execute(f"SELECT COUNT(*) FROM {dbs.BOOKKEEPING_TABLE}").fetchone()[0], 3)
            conn.close()

    def test_rebuild_is_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            conf = make_conf(Path(td))
            dbs.local_schema(conf, "main-db", "dev")
            scratch = dbs.local_schema(conf, "main-db", "dev")  # idempotent: fresh file
            conn = sqlite3.connect(scratch)
            self.assertEqual(
                conn.execute(f"SELECT COUNT(*) FROM {dbs.BOOKKEEPING_TABLE}").fetchone()[0],
                2,
            )
            conn.close()


class TestRenderers(unittest.TestCase):
    def test_litestream_yaml_lists_all_dbs(self):
        dbs_list = [
            Db(name="main-db", migrations="/m", replication=Replication(
                bucket="backups", endpoint="https://acct.r2.cloudflarestorage.com",
                region="us-east-1",
                access_key_env="R2_ACCESS_KEY_ID", secret_key_env="R2_SECRET_ACCESS_KEY",
                sync_interval="1s", retention="720h")),
            Db(name="analytics", migrations="/m", replication=Replication(
                bucket="backups")),
        ]
        out = litestream.render_litestream_yaml(dbs_list)
        self.assertTrue(out.startswith("# rendered by anci"))
        doc = json.loads(out.split("\n", 1)[1])   # JSON is valid YAML
        # the control socket status reads sync state from (no S3 round trip)
        self.assertEqual(doc["socket"], {"enabled": True, "path": "/run/litestream/litestream.sock"})
        main, analytics = doc["dbs"]
        self.assertEqual(main["path"], "/var/lib/anci/db/main-db/data.sql")
        self.assertEqual(analytics["path"], "/var/lib/anci/db/analytics/data.sql")
        # constituent keys, not a url; creds are env var NAMES litestream expands
        self.assertEqual(main["replicas"], [{
            "type": "s3", "bucket": "backups", "path": "main-db",
            "endpoint": "https://acct.r2.cloudflarestorage.com", "region": "us-east-1",
            "access-key-id": "${R2_ACCESS_KEY_ID}",
            "secret-access-key": "${R2_SECRET_ACCESS_KEY}",
            "sync-interval": "1s", "retention": "720h"}])
        # a plain-AWS db renders no endpoint / region / key lines; path defaults to the name
        self.assertEqual(analytics["replicas"], [{"type": "s3", "bucket": "backups",
                                                  "path": "analytics"}])

    def test_litestream_unit(self):
        unit = systemd.render_litestream_unit()
        self.assertIn("User=anci", unit)   # shared service user, not root
        # the unprivileged user needs a writable dir for the control socket
        self.assertIn("RuntimeDirectory=litestream", unit)
        self.assertIn("ExecStart=/usr/local/bin/litestream replicate -config /etc/anci/litestream.yaml", unit)
        self.assertIn("Restart=on-failure", unit)

    def test_replica_target_is_bucket_slash_path(self):
        db = Db(name="x", migrations="/m",
                replication=Replication(bucket="b", path="p"))
        self.assertEqual(litestream.replica_target(db), "b/p")
        self.assertEqual(litestream.replica_target(
            Db(name="x", migrations="/m", replication=Replication(bucket="b"))), "b/x")


class TestValidation(unittest.TestCase):
    def test_bad_replication_rejected(self):
        import tomllib

        bad = """
[db.x]
migrations = '/m'
replication = 's3://bucket'
"""
        from anci.conf import load

        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(bad)
            fd = f
        fd.close()
        with self.assertRaises(errors.ConfError):
            load(fd.name)
class TestMigrateVerify(unittest.TestCase):
    """The post-migrate check reads the db: only quick_check, only after a change."""

    def _cmds(self, td, migrations):
        conf = TestMigrateCreatesOwner._conf(None, Path(td))
        cmds: list[str] = []

        def _run(conn, s, **k):
            cmds.append(s)
            return ssh.Result("main-db quick\tok\n" if "quick_check" in s else "", "")

        with mock.patch.object(dbs.auth, "authorize", return_value="jack"), \
             mock.patch.object(dbs.ssh, "connect", return_value=mock.Mock()), \
             mock.patch.object(dbs.ssh, "wait_ready"), \
             mock.patch.object(dbs.ssh, "test", return_value=True), \
             mock.patch.object(dbs.ssh, "sudo", side_effect=_run), \
             mock.patch.object(dbs.ssh, "script", side_effect=_run), \
             mock.patch.object(dbs.ssh, "put"), \
             mock.patch.object(dbs, "anchored_migrations", return_value=migrations), \
             mock.patch("anci.converge.converge"), \
             contextlib.redirect_stdout(io.StringIO()):
            dbs.migrate(conf, "main-db", "dev")
        return " ".join(cmds)

    def test_quick_check_after_applying(self):
        with tempfile.TemporaryDirectory() as td:
            mig = Path(td) / "0001.sql"
            mig.write_text("CREATE TABLE t (id INTEGER);")
            cmds = self._cmds(td, [("0001", mig)])
        self.assertIn("PRAGMA quick_check", cmds)
        self.assertNotIn("integrity_check", cmds)

    def test_no_check_when_nothing_applied(self):
        with tempfile.TemporaryDirectory() as td:
            cmds = self._cmds(td, [])
        self.assertNotIn("_check", cmds)


class TestMigrateCreatesOwner(unittest.TestCase):
    """Regression: `migrate` runs *before* `deploy` by convention, so it
    must ensure the shared `anci` service user (the db owner) exists itself — a
    fresh box would otherwise fail at `install -d -o anci`."""

    def _conf(self, tmp: Path) -> Conf:
        mig = tmp / "migrations"
        mig.mkdir(exist_ok=True)
        (mig / "0001.sql").write_text("CREATE TABLE t (id INTEGER PRIMARY KEY);")
        return Conf(
            users={"jack": User(name="jack", ssh_pub_keys=["k"])},
            envs={"dev": Env(name="dev", ip="127.0.0.1", access_users=["jack"],
                             anci_version="0.0.1")},
            exes={"app": Exe(name="app", source="./bin/app",
                             env_overrides={"dev": ExeOverride(env="dev")})},
            dbs={"main-db": Db(name="main-db", migrations=str(mig))},
        )

    def test_creates_owner_before_using_it(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(Path(td))
            cmds: list[str] = []

            def _sudo(conn, s, **k):
                cmds.append(s)
                return ssh.Result("", "")

            def _script(conn, s, **k):
                cmds.append(s)
                return ssh.Result("main-db quick\tok\n" if "quick_check" in s else "", "")

            with mock.patch.object(dbs.auth, "authorize", return_value="jack"), \
                 mock.patch.object(dbs.ssh, "connect", return_value=mock.Mock()), \
                 mock.patch.object(dbs.ssh, "wait_ready"), \
                 mock.patch.object(dbs.ssh, "test", return_value=True), \
                 mock.patch.object(dbs.ssh, "sudo", side_effect=_sudo), \
                 mock.patch.object(dbs.ssh, "script", side_effect=_script), \
                 mock.patch.object(dbs.ssh, "put"), \
                 mock.patch("anci.converge.converge"), \
                 contextlib.redirect_stdout(io.StringIO()):
                dbs.migrate(conf, "main-db", "dev")

        prep = next((c for c in cmds if "useradd --system --no-create-home" in c), None)
        self.assertIsNotNone(prep, f"no service-user ensure; commands: {cmds}")
        for part in ("getent group anci", "install -d -o root -g anci -m 2775",
                     "install -d -o anci -g anci -m 0750"):
            self.assertIn(part, prep)
        # the group + owner exist before anything requires them
        self.assertLess(prep.index("getent group anci"), prep.index("install -d -o root -g anci -m 2775"))
        self.assertLess(prep.index("useradd --system"), prep.index("install -d -o anci -g anci -m 0750"))



class TestUserTablesSql(unittest.TestCase):
    """What counts as a foreign schema: litestream's own tables must not."""

    def _found(self, *tables):
        db = sqlite3.connect(":memory:")
        for t in tables:
            db.execute(f"CREATE TABLE {t} (x)")
        return [r[0] for r in db.execute(dbs._USER_TABLES_SQL)]

    def test_litestream_tables_are_not_a_schema(self):
        self.assertEqual(self._found("_litestream_seq", "_litestream_lock"), [])

    def test_anci_bookkeeping_is_not_a_schema(self):
        self.assertEqual(self._found(dbs.BOOKKEEPING_TABLE), [])

    def test_user_table_is_found_beside_litestream(self):
        self.assertEqual(self._found("_litestream_seq", "notes"), ["notes"])

    def test_lookalike_name_is_found(self):
        self.assertEqual(self._found("xlitestreamy"), ["xlitestreamy"])


class TestMigrateChecksums(unittest.TestCase):
    """Checksummed bookkeeping: edited/removed migrations, --baseline, old schema."""

    def _conf(self, tmp: Path) -> Conf:
        mig = tmp / "migrations"
        mig.mkdir(exist_ok=True)
        (mig / "0001_create.sql").write_text("CREATE TABLE notes (id INTEGER);")
        return Conf(
            users={"jack": User(name="jack", ssh_pub_keys=["k"])},
            envs={"dev": Env(name="dev", ip="127.0.0.1", access_users=["jack"],
                             anci_version="0.0.1")},
            exes={"app": Exe(name="app", source="./bin/app",
                             env_overrides={"dev": ExeOverride(env="dev")})},
            dbs={"main-db": Db(name="main-db", migrations=str(mig))},
        )

    def _run(self, conf, *, cols, applied=None, baseline=False, has_tables=False):
        upgrade, setc, delc = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        with mock.patch.object(dbs.auth, "authorize", return_value="jack"), \
             mock.patch.object(dbs.ssh, "connect", return_value=mock.Mock()), \
             mock.patch.object(dbs.ssh, "wait_ready"), \
             mock.patch.object(dbs.ssh, "test", return_value=True), \
             mock.patch.object(dbs.ssh, "sudo"), \
             mock.patch.object(dbs.ssh, "script", side_effect=lambda c, s, **k: ssh.Result(
                 "main-db quick\tok\n" if "quick_check" in s else "", "")), \
             mock.patch.object(dbs.ssh, "put"), \
             mock.patch.object(dbs, "_read_history", return_value={
                 "main-db": (cols, ["notes"] if has_tables else [], applied or {})}), \
             mock.patch.object(dbs, "_applied_checksums", return_value=applied or {}), \
             mock.patch.object(dbs, "_upgrade_bookkeeping", upgrade), \
             mock.patch.object(dbs, "_set_checksums", setc), \
             mock.patch.object(dbs, "_delete_bookkeeping", delc), \
             mock.patch("anci.converge.converge"), \
             contextlib.redirect_stdout(io.StringIO()):
            dbs.migrate(conf, "main-db", "dev", baseline=baseline)
        return upgrade, setc, delc

    def test_edited_migration_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(Path(td))
            with self.assertRaises(errors.EnvError) as cm:
                self._run(conf, cols={"name", "checksum", "ts"},
                          applied={"0001_create": "deadbeef"})
        self.assertIn("changed after", str(cm.exception))

    def test_removed_migration_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(Path(td))
            with self.assertRaises(errors.EnvError) as cm:
                self._run(conf, cols={"name", "checksum", "ts"},
                          applied={"9999_gone": "deadbeef"})
        self.assertIn("no longer on disk", str(cm.exception))

    def test_old_bookkeeping_requires_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(Path(td))
            with self.assertRaises(errors.EnvError) as cm:
                self._run(conf, cols={"name", "ts"})
        self.assertIn("predates checksums", str(cm.exception))

    def test_schema_without_bookkeeping_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(Path(td))
            with self.assertRaises(errors.EnvError) as cm:
                self._run(conf, cols=None, has_tables=True)
        self.assertIn("no anci migration bookkeeping", str(cm.exception))

    def test_baseline_upgrades_old_bookkeeping(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(Path(td))
            upgrade, _setc, _delc = self._run(
                conf, cols={"name", "ts"}, applied={"0001_create": None}, baseline=True)
        upgrade.assert_called_once()

    def test_baseline_accepts_edited_files(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(Path(td))
            _u, setc, _d = self._run(
                conf, cols={"name", "checksum", "ts"},
                applied={"0001_create": "deadbeef"}, baseline=True)
        setc.assert_called_once()
        self.assertIn("0001_create", setc.call_args[0][2])

    def test_baseline_forgets_removed_files(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(Path(td))
            _u, _s, delc = self._run(
                conf, cols={"name", "checksum", "ts"},
                applied={"9999_gone": "deadbeef"}, baseline=True)
        delc.assert_called_once()
        self.assertEqual(delc.call_args[0][2], ["9999_gone"])


if __name__ == "__main__":
    unittest.main()


class TestMigrateAll(unittest.TestCase):
    """`migrate` with no db: every db in name order, then one converge."""

    def _run(self, conf):
        order, scripts = [], []

        def _put(conn, local, remote, **k):
            order.append(Path(local).read_bytes().decode())   # the bundle

        def _script(conn, text, **k):
            scripts.append(text)
            return ssh.Result("aux quick\tok\nmain-db quick\tok\n" if "quick_check" in text else "", "")

        with mock.patch.object(dbs.auth, "authorize", return_value="jack"), \
             mock.patch.object(dbs.ssh, "connect", return_value=ssh.Conn(ip="x")), \
             mock.patch.object(dbs.ssh, "wait_ready"), \
             mock.patch.object(dbs.boxlock, "hold"), \
             mock.patch.object(dbs.ssh, "sudo", return_value=ssh.Result("", "")), \
             mock.patch.object(dbs.ssh, "put", side_effect=_put), \
             mock.patch.object(dbs.ssh, "script", side_effect=_script), \
             mock.patch("anci.converge.converge",
                        side_effect=lambda *a, **k: order.append("converge")), \
             contextlib.redirect_stdout(io.StringIO()):
            dbs.migrate(conf, None, "dev")
        return order, scripts

    def test_every_db_in_one_bundle_then_one_converge(self):
        with tempfile.TemporaryDirectory() as td:
            conf = TestMigrateCreatesOwner._conf(None, Path(td))
            conf.dbs["aux"] = Db(name="aux", migrations=str(Path(td) / "migrations"))
            order, scripts = self._run(conf)
        self.assertEqual(len(order), 2)                 # one upload, then the converge
        bundle, converge = order
        self.assertEqual(converge, "converge")
        self.assertLess(bundle.index("db/aux/data.sql < "), bundle.index("db/main-db/data.sql < "))
        # history and verification are one round trip each, for both dbs
        self.assertEqual(len([s for s in scripts if "pragma_table_info" in s]), 1)
        self.assertEqual(len([s for s in scripts if "quick_check" in s]), 1)

    def test_no_dbs_is_conf_class(self):
        with tempfile.TemporaryDirectory() as td:
            conf = TestMigrateCreatesOwner._conf(None, Path(td))
            conf.dbs.clear()
            with self.assertRaises(errors.ConfError) as cm:
                self._run(conf)
        self.assertIn("no dbs", str(cm.exception))


class TestMigrateFailure(unittest.TestCase):
    """A failing migration names itself and what this run already committed."""

    def _migrate(self, td, applied_after):
        conf = make_conf(Path(td))
        next(iter(conf.dbs.values())).extensions = []   # not this test's concern
        files = []
        for name in ("0001_a", "0002_b"):
            p = Path(td) / f"{name}.sql"
            p.write_text("CREATE TABLE t (id INTEGER);")
            files.append((name, p))

        def _script(conn, text, *, timeout=120):
            if text.startswith("bash "):
                raise ssh.SSHError("bundle failed",
                                   stderr="Error: near line 3\nanci-failed main-db")
            return ssh.Result("", "")

        with mock.patch.object(dbs.auth, "authorize", return_value="jack"), \
             mock.patch.object(dbs.ssh, "connect", return_value=ssh.Conn(ip="x")), \
             mock.patch.object(dbs.ssh, "wait_ready"), \
             mock.patch.object(dbs.ssh, "sudo", return_value=ssh.Result("", "")), \
             mock.patch.object(dbs.ssh, "put"), \
             mock.patch.object(dbs.ssh, "script", side_effect=_script), \
             mock.patch.object(dbs.apply_mod, "guest_sha", return_value=None), \
             mock.patch.object(dbs, "anchored_migrations", return_value=files), \
             mock.patch.object(dbs, "_applied_checksums", return_value=applied_after), \
             mock.patch.object(dbs, "scratch") as scratch, \
             contextlib.redirect_stdout(io.StringIO()):
            scratch.local.return_value = Path(td) / "script"
            scratch.remote.return_value = "/tmp/script"
            with self.assertRaises(errors.EnvError) as cm:
                dbs.migrate(conf, next(iter(conf.dbs)), "dev")
        return cm.exception

    def test_second_fails_after_first_committed(self):
        with tempfile.TemporaryDirectory() as td:
            err = self._migrate(td, {"0001_a": "x"})
        self.assertIn("migration 0002_b failed", str(err))
        self.assertIn("applied in this run before it: 0001_a", err.why)

    def test_first_fails_nothing_applied(self):
        with tempfile.TemporaryDirectory() as td:
            err = self._migrate(td, {})
        self.assertIn("migration 0001_a failed", str(err))
        self.assertIn("nothing was applied", err.why)


class TestMigrateVerify(unittest.TestCase):
    """After applying migrations: quick_check, optimize, then foreign_key_check."""

    def _migrate(self, td, fk_rows):
        conf = make_conf(Path(td))
        next(iter(conf.dbs.values())).extensions = []   # not this test's concern
        p = Path(td) / "0001_a.sql"
        p.write_text("CREATE TABLE t (id INTEGER);")
        seen = []

        def _script(conn, text, *, timeout=120):
            seen.append(text)
            if "foreign_key_check" in text:
                return ssh.Result("main-db quick\tok\n" + "".join(
                    f"main-db fk\t{row}\n" for row in fk_rows.splitlines()), "")
            return ssh.Result("", "")

        with mock.patch.object(dbs.auth, "authorize", return_value="jack"), \
             mock.patch.object(dbs.ssh, "connect", return_value=ssh.Conn(ip="x")), \
             mock.patch.object(dbs.ssh, "wait_ready"), \
             mock.patch.object(dbs.ssh, "sudo", return_value=ssh.Result("", "")), \
             mock.patch.object(dbs.ssh, "put"), \
             mock.patch.object(dbs.ssh, "script", side_effect=_script), \
             mock.patch.object(dbs.apply_mod, "guest_sha", return_value=None), \
             mock.patch.object(dbs, "anchored_migrations", return_value=[("0001_a", p)]), \
             mock.patch.object(dbs, "scratch") as scratch, \
             mock.patch("anci.converge.converge") as converge, \
             contextlib.redirect_stdout(io.StringIO()):
            scratch.local.return_value = Path(td) / "script"
            scratch.remote.return_value = "/tmp/script"
            try:
                dbs.migrate(conf, next(iter(conf.dbs)), "dev")
                err = None
            except errors.EnvError as e:
                err = e
        return err, seen, converge

    def test_clean_db_is_optimized_and_converged(self):
        with tempfile.TemporaryDirectory() as td:
            err, seen, converge = self._migrate(td, "")
        self.assertIsNone(err)
        check = next(t for t in seen if "quick_check" in t)        # one round trip, in order
        self.assertLess(check.index("quick_check"), check.index("optimize"))
        self.assertLess(check.index("optimize"), check.index("foreign_key_check"))
        self.assertGreaterEqual(check.count(f".timeout {dbs.SQLITE_TIMEOUT_MS}"), 3)
        converge.assert_called_once()

    def test_foreign_key_violations_name_the_tables(self):
        with tempfile.TemporaryDirectory() as td:
            err, _seen, converge = self._migrate(td, "review|7|entity|0\nreview|9|entity|0\ntag|3|review|0\n")
        self.assertIn("foreign key violations in review, tag", str(err))
        self.assertIn("3 row(s)", err.why)
        converge.assert_not_called()


@unittest.skipUnless(shutil.which("sqlite3"), "needs the sqlite3 CLI")
class TestMigrationScript(unittest.TestCase):
    """The script `migrate` runs on the box, run here through a real sqlite3."""

    def _run(self, td, db_file, migrations):
        files = []
        for name, sql in migrations:
            p = Path(td) / f"{name}.sql"
            p.write_text(sql)
            files.append((name, p))
        checksums = {name: dbs._checksum(p) for name, p in files}
        script = dbs._migration_script("main-db", files, [], checksums)
        # the box reads each migration from its scratch copy; point those at the local files
        for name, p in files:
            script = script.replace(dbs.mig_remote("main-db", name), str(p))
        # the command the box runs, minus its `sudo -n -u anci`
        argv = shlex.split(dbs.sqlite3_cmd(str(db_file), bail=True))[4:]
        return subprocess.run(argv, input=script, capture_output=True, text=True)

    def _mode(self, db_file):
        return sqlite3.connect(db_file).execute("PRAGMA journal_mode").fetchone()[0]

    def test_wal_is_set_before_any_migration_transaction(self):
        script = dbs._migration_script("main-db", [("0001_a", Path("x"))], ["/ext/vec0.so"], {"0001_a": "c"})
        lines = script.splitlines()
        self.assertEqual(lines[0], ".load /ext/vec0.so")
        self.assertLess(lines.index("PRAGMA journal_mode=WAL;"), lines.index("BEGIN;"))

    def test_new_db_ends_up_wal_with_migrations_applied(self):
        with tempfile.TemporaryDirectory() as td:
            db_file = Path(td) / "data.sql"
            r = self._run(td, db_file, [("0001_a", "CREATE TABLE a (id INTEGER);"),
                                        ("0002_b", "INSERT INTO a VALUES (1);")])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self._mode(db_file), "wal")
            conn = sqlite3.connect(db_file)
            self.assertEqual(conn.execute("SELECT id FROM a").fetchall(), [(1,)])
            self.assertEqual([n for (n,) in conn.execute(
                f"SELECT name FROM {dbs.BOOKKEEPING_TABLE} ORDER BY name")], ["0001_a", "0002_b"])

    def test_db_already_in_wal_migrates_again(self):
        with tempfile.TemporaryDirectory() as td:
            db_file = Path(td) / "data.sql"
            self._run(td, db_file, [("0001_a", "CREATE TABLE a (id INTEGER);")])
            r = self._run(td, db_file, [("0002_b", "CREATE TABLE b (id INTEGER);")])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self._mode(db_file), "wal")

    def test_new_db_gets_incremental_auto_vacuum(self):
        with tempfile.TemporaryDirectory() as td:
            db_file = Path(td) / "data.sql"
            r = self._run(td, db_file, [("0001_a", "CREATE TABLE a (id INTEGER);")])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(sqlite3.connect(db_file).execute("PRAGMA auto_vacuum").fetchone()[0], 2)

    def test_existing_db_keeps_its_auto_vacuum(self):
        # it only takes on a db with no tables; an existing one migrates on unchanged
        with tempfile.TemporaryDirectory() as td:
            db_file = Path(td) / "data.sql"
            sqlite3.connect(db_file).execute("CREATE TABLE pre (id INTEGER)").connection.close()
            r = self._run(td, db_file, [("0001_a", "CREATE TABLE a (id INTEGER);")])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(sqlite3.connect(db_file).execute("PRAGMA auto_vacuum").fetchone()[0], 0)
            self.assertEqual(self._mode(db_file), "wal")

    def test_waits_for_a_lock_an_app_holds(self):
        # apps keep running through `migrate`: a write in progress must delay the migration, not fail it
        with tempfile.TemporaryDirectory() as td:
            db_file = Path(td) / "data.sql"
            self._run(td, db_file, [("0001_a", "CREATE TABLE a (id INTEGER);")])
            app = sqlite3.connect(db_file, isolation_level=None, check_same_thread=False)
            app.execute("BEGIN IMMEDIATE")
            app.execute("INSERT INTO a VALUES (1)")
            release = threading.Timer(0.5, app.commit)
            release.start()
            try:
                r = self._run(td, db_file, [("0002_b", "INSERT INTO a VALUES (2);")])
            finally:
                release.join()
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(sqlite3.connect(db_file).execute("SELECT id FROM a ORDER BY id").fetchall(),
                             [(1,), (2,)])

    def test_journal_mode_inside_a_migration_still_fails(self):
        # the reason anci owns journal_mode: within the transaction it can't change
        with tempfile.TemporaryDirectory() as td:
            r = self._run(td, Path(td) / "data.sql",
                          [("0001_a", "PRAGMA journal_mode=DELETE;\nCREATE TABLE a (id INTEGER);")])
            self.assertNotEqual(r.returncode, 0)


@unittest.skipUnless(shutil.which("sqlite3") and shutil.which("bash"), "needs sqlite3 and bash")
class TestMigrationBundle(unittest.TestCase):
    """The one script `migrate` ships for every db, run here through real bash + sqlite3."""

    def _runs(self, td, dbs_migrations):
        runs = []
        for db_name, migrations in dbs_migrations:
            files = []
            for name, sql in migrations:
                f = Path(td) / f"{db_name}-{name}.sql"
                f.write_text(sql)
                files.append((name, f))
            runs.append(dbs._DbRun(name=db_name, db=Db(name=db_name, migrations=td),
                                   file=str(Path(td) / f"{db_name}.db"), migrations=files,
                                   checksums={n: dbs._checksum(f) for n, f in files},
                                   pending=files))
        return runs

    def _bash(self, runs):
        script = dbs._bundle(runs, "/dev/null").decode().replace(f"sudo -n -u {dbs.ANCI_USER} ", "")
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def _names(self, db_file):
        return [n for (n,) in sqlite3.connect(db_file).execute(
            f"SELECT name FROM {dbs.BOOKKEEPING_TABLE} ORDER BY name")]

    def test_every_db_migrates_in_one_run(self):
        with tempfile.TemporaryDirectory() as td:
            runs = self._runs(td, [("a", [("0001", "CREATE TABLE x (id INTEGER)")]),   # no semicolon
                                   ("b", [("0001", "CREATE TABLE y (id INTEGER);"),
                                          ("0002", "INSERT INTO y VALUES (1);")])])
            r = self._bash(runs)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self._names(runs[0].file), ["0001"])
            self.assertEqual(self._names(runs[1].file), ["0001", "0002"])
            self.assertFalse(Path(dbs.MIG_DIR).exists())                 # scratch cleaned up

    def test_a_failing_db_stops_the_run_and_names_itself(self):
        with tempfile.TemporaryDirectory() as td:
            runs = self._runs(td, [("a", [("0001", "CREATE TABLE x (id INTEGER);")]),
                                   ("b", [("0001", "CREATE TABLE y (id INTEGER);"),
                                          ("0002", "NOT SQL;")]),
                                   ("c", [("0001", "CREATE TABLE z (id INTEGER);")])])
            r = self._bash(runs)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("anci-failed b", r.stderr)
            self.assertEqual(self._names(runs[0].file), ["0001"])        # before it: kept
            self.assertEqual(self._names(runs[1].file), ["0001"])        # its earlier migration: kept
            self.assertFalse(Path(runs[2].file).exists())                # after it: untouched

    def test_heredoc_survives_its_own_delimiter_in_the_content(self):
        content = b"SELECT 1;\nANCI_EOF_x\n"
        out = dbs._heredoc("/tmp/f", content)
        delim = out.splitlines()[0].decode().split("<<'")[1].rstrip("'")
        self.assertNotIn(delim.encode() + b"\n", content)
        self.assertTrue(out.endswith(f"{delim}\n".encode()))


class TestVersionPins(unittest.TestCase):
    def test_every_entry_pins_a_hash_for_every_download(self):
        from anci import versions
        for v, e in versions.load().items():
            self.assertTrue(e.litestream_sha256, v)
            self.assertTrue(e.caddy_sha512, v)
            self.assertRegex(e.sqlite_sha256 or "", r"^[0-9a-f]{64}$", v)
            self.assertTrue(e.sqlite_url, v)
            self.assertTrue(e.sops, v)
            # one sops build per platform the anci CLI ships for
            self.assertEqual(set(e.sops_builds), {"linux-x86_64", "linux-aarch64",
                                                  "macos-aarch64", "macos-x86_64"}, v)
            for url, sha256 in e.sops_builds.values():
                self.assertRegex(sha256, r"^[0-9a-f]{64}$", v)
                self.assertIn(f"v{e.sops}", url, v)


class TestRegenTimestamp(unittest.TestCase):
    def _conf(self):
        return Conf(
            users={"jack": User(name="jack", ssh_pub_keys=["k"])},
            envs={"dev": Env(name="dev", ip="127.0.0.1", access_users=["jack"],
                         anci_version="0.0.1")},
            exes={}, dbs={"main-db": Db(name="main-db", migrations="/m",
                                        replication=Replication(bucket="b"))},
        )

    def test_script_restores_latest_by_default(self):
        self.assertNotIn("-timestamp", dbs._restore_script("main-db"))

    def test_script_passes_the_timestamp(self):
        script = dbs._restore_script("main-db", "2026-10-14T15:00:00Z")
        self.assertIn("restore -config /etc/anci/litestream.yaml "
                      "-timestamp 2026-10-14T15:00:00Z -o ", script)

    def test_timestamp_is_normalized_to_utc(self):
        self.assertEqual(dbs._parse_timestamp("2026-10-14T11:00:00-04:00"),
                         "2026-10-14T15:00:00Z")

    def test_bad_timestamps_are_refused_before_touching_the_box(self):
        for bad in ("yesterday", "2026-10-14T15:00:00", "2026-10-14"):
            with mock.patch.object(dbs.ssh, "connect") as connect:
                with self.assertRaises(errors.ConfError):
                    dbs.db_regen(self._conf(), "main-db", "dev",
                                 force=True, timestamp=bad)
            connect.assert_not_called()

    PLAN = ("live=53248\n" + json.dumps({"files": [
        {"level": 9, "size": 40000, "timestamp": "2026-10-14T15:00:00Z"},
        {"level": 1, "size": 5000, "timestamp": "2026-10-14T15:30:00Z"}]}))

    def _regen(self, *, force=True, answer=None, plan=PLAN, timestamp=None, sudo_error=None):
        calls = []

        def sudo(conn, script, **kw):
            calls.append(script)
            if "-dry-run" in script:
                if sudo_error:
                    raise sudo_error
                return ssh.Result(plan, "")
            return ssh.Result("", "")

        out = io.StringIO()
        with mock.patch.object(dbs.auth, "authorize", return_value="jack"), \
             mock.patch.object(dbs.ssh, "connect", return_value=mock.Mock()), \
             mock.patch.object(dbs.ssh, "sudo", sudo), \
             mock.patch.object(dbs.lifecycle, "stop_app_units") as stop, \
             mock.patch.object(dbs.lifecycle, "start_conf_units"), \
             mock.patch.object(dbs.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", return_value=answer or "y") as ask, \
             contextlib.redirect_stdout(out):
            try:
                dbs.db_regen(self._conf(), "main-db", "dev", force=force, timestamp=timestamp)
            finally:
                self.out, self.calls, self.stop, self.ask = out.getvalue(), calls, stop, ask

    def test_valid_timestamp_reaches_plan_and_restore(self):
        self._regen(timestamp="2026-10-14T15:00:00Z")
        self.assertEqual(len(self.calls), 2)
        for script in self.calls:
            self.assertIn("-timestamp 2026-10-14T15:00:00Z", script)

    def test_plan_script_is_read_only(self):
        script = dbs._plan_script("main-db")
        self.assertIn("-dry-run -json", script)
        self.assertIn("live=", script)
        self.assertNotIn("systemctl", script)
        self.assertNotIn("-timestamp", script)

    def test_parse_plan_sums_sizes_and_takes_newest_timestamp(self):
        self.assertEqual(dbs._parse_plan(self.PLAN),
                         (53248, 45000, "2026-10-14T15:30:00Z"))

    def test_parse_plan_survives_garbage(self):
        for bad in ("", "nope", "live=abc\n{}", "live=1\nnot json", "live=1\n{\"files\": 3}"):
            self.assertIsNone(dbs._parse_plan(bad), bad)

    def test_summary_shows_both_sizes_and_prompt_follows(self):
        self._regen(force=False, answer="y")
        self.assertIn("at least 43.9 KB", self.out)
        self.assertIn("live db is 52.0 KB", self.out)
        self.ask.assert_called_once()
        self.stop.assert_called_once()

    def test_answering_no_changes_nothing(self):
        self._regen(force=False, answer="n")
        self.assertIn("aborted", self.out)
        self.stop.assert_not_called()
        self.assertEqual(len(self.calls), 1)   # only the read-only plan ran

    def test_force_prints_the_summary_without_asking(self):
        self._regen(force=True)
        self.assertIn("at least", self.out)
        self.ask.assert_not_called()

    def test_unreadable_plan_still_regens(self):
        self._regen(plan="garbage")
        self.assertNotIn("at least", self.out)
        self.stop.assert_called_once()

    def test_failing_plan_stops_before_anything_is_touched(self):
        err = dbs.ssh.SSHError("restore failed", stderr="no matching backup files available")
        with self.assertRaises(errors.EnvError):
            self._regen(timestamp="2000-01-01T00:00:00Z", sudo_error=err)
        self.stop.assert_not_called()
        self.ask.assert_not_called()



A_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAa"
B_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBb"
C_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCc"


def _sops_file(*keys):
    """The metadata lines sops writes into an encrypted dotenv, one list entry per recipient."""
    lines = ["K=ENC[AES256_GCM,data:xx,iv:yy,tag:zz,type:str]"]
    for i, k in enumerate(keys):
        lines += [f"sops_age__list_{i}__map_enc=-----BEGIN AGE ENCRYPTED FILE-----\\nabc",
                  f"sops_age__list_{i}__map_recipient={k}"]
    lines += ["sops_lastmodified=2026-10-15T14:13:32Z", "sops_mac=ENC[AES256_GCM,data:m]"]
    return "\n".join(lines) + "\n"


class TestRecipientChanges(unittest.TestCase):
    def test_nothing_to_do_when_the_sets_match(self):
        self.assertEqual(secrets_mod.recipient_changes(_sops_file(A_KEY, B_KEY), [B_KEY, A_KEY]), ([], []))

    def test_comment_differences_are_not_changes(self):
        self.assertEqual(secrets_mod.recipient_changes(_sops_file(A_KEY), [A_KEY + " jack@dev-machine"]),
                         ([], []))

    def test_new_key_is_added_and_a_dropped_one_removed(self):
        add, rm = secrets_mod.recipient_changes(_sops_file(A_KEY, B_KEY), [A_KEY, C_KEY + " jack@x"])
        self.assertEqual(add, [C_KEY])        # sent to sops without the comment
        self.assertEqual(rm, [B_KEY])

    def test_file_with_no_metadata_reads_as_empty(self):
        self.assertEqual(secrets_mod.recipient_changes("K=1\n", [A_KEY]), ([A_KEY], []))


class TestSecretsEditSyncsRecipients(unittest.TestCase):
    """`sops edit --age` never changes an existing file's recipients — anci must."""

    def _run(self, file_keys, conf_keys, *, rotate_rc=0, edit_rc=0):
        import os
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=rotate_rc if argv[1] == "rotate" else edit_rc)

        with tempfile.TemporaryDirectory() as td:
            conf = Conf(
                users={"jack": User(name="jack", ssh_pub_keys=list(conf_keys))},
                envs={"dev": Env(name="dev", ip="127.0.0.1", access_users=["jack"],
                                 anci_version="0.0.1")},
                exes={}, dbs={}, path=os.path.join(td, "anci.toml"),
            )
            enc = Path(td) / "envs" / "dev" / "secrets.env"
            enc.parent.mkdir(parents=True)
            if file_keys is not None:
                enc.write_text(_sops_file(*file_keys))
            out = io.StringIO()
            with mock.patch.object(dbs.sys, "stdin") as stdin, \
                 mock.patch.object(dbs.auth, "authorize", return_value="jack"), \
                 mock.patch.object(dbs.secrets_mod, "sops_binary", return_value=Path("/x/sops")), \
                 mock.patch.object(dbs.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(secrets_mod.subprocess, "run", side_effect=fake_run), \
                 contextlib.redirect_stdout(out):
                stdin.isatty.return_value = True
                try:
                    dbs.secrets_edit(conf, "dev")
                finally:
                    self.calls, self.out = calls, out.getvalue()

    def test_added_key_is_rotated_in_before_the_edit(self):
        self._run([A_KEY], [A_KEY, B_KEY])
        self.assertEqual([c[1] for c in self.calls], ["rotate", "edit"])
        rotate = self.calls[0]
        self.assertEqual(rotate[2], "-i")
        self.assertEqual(rotate[rotate.index("--add-age") + 1], B_KEY)
        self.assertNotIn("--rm-age", rotate)
        self.assertIn("+1 added, -0 removed", self.out)

    def test_closing_the_editor_unchanged_is_not_an_error(self):
        self._run([A_KEY], [A_KEY], edit_rc=dbs.SOPS_UNCHANGED)
        self.assertIn("no changes", self.out)
        self.assertNotIn("secrets saved", self.out)

    def test_a_real_sops_failure_is_still_an_error(self):
        with self.assertRaises(errors.InternalError):
            self._run([A_KEY], [A_KEY], edit_rc=1)

    def test_removed_key_is_rotated_out(self):
        self._run([A_KEY, B_KEY], [A_KEY])
        rotate = self.calls[0]
        self.assertEqual(rotate[rotate.index("--rm-age") + 1], B_KEY)

    def test_unchanged_recipients_skip_the_rotate(self):
        self._run([A_KEY, B_KEY], [B_KEY, A_KEY])
        self.assertEqual([c[1] for c in self.calls], ["edit"])

    def test_new_file_is_created_with_the_conf_keys(self):
        self._run(None, [A_KEY, B_KEY])
        self.assertEqual([c[1] for c in self.calls], ["edit"])
        # one comma-separated --age: sops keeps only the last of repeated flags
        edit = self.calls[0]
        self.assertEqual(edit.count("--age"), 1)
        self.assertEqual(sorted(edit[edit.index("--age") + 1].split(",")),
                         sorted(" ".join(k.split()[:2]) for k in (A_KEY, B_KEY)))

    def test_a_machine_that_cannot_decrypt_gets_a_clear_error_and_no_edit(self):
        with self.assertRaises(errors.AuthError) as cm:
            self._run([A_KEY], [A_KEY, B_KEY], rotate_rc=1)
        self.assertIn("already a recipient", cm.exception.try_)
        self.assertEqual([c[1] for c in self.calls], ["rotate"])


class TestMigrateExtensions(unittest.TestCase):
    """migrate ships extensions as blobs symlinked into the ext dir, pruning stale ones."""

    class _Stop(Exception):
        pass

    def test_extension_becomes_a_blob_and_symlink(self):
        from anci import layout
        from anci.apply import sha256_file
        with tempfile.TemporaryDirectory() as td:
            conf = make_conf(Path(td))
            ext = Path(td) / "ext.so"
            ext.write_bytes(b"\x7fELF-ext")
            sha = sha256_file(ext)
            sudos, applied_links, uploaded = [], [], []

            def _upload_blob(conn, blob, have_size, exists=None):
                uploaded.append(blob)
                return True

            def _apply(conn, files, links, probe_result=None, deletes=None):
                applied_links.extend(links)
                raise self._Stop()     # stop migrate right after the extension step

            with mock.patch.object(dbs.auth, "authorize", return_value="jack"), \
                 mock.patch.object(dbs.ssh, "connect", return_value=ssh.Conn(ip="x")), \
                 mock.patch.object(dbs.ssh, "wait_ready"), \
                 mock.patch.object(dbs.ssh, "sudo",
                                   side_effect=lambda c, s, **k: sudos.append(s) or ssh.Result("", "")), \
                 mock.patch.object(dbs.ssh, "put"), \
                 mock.patch.object(dbs.ssh, "script", return_value=ssh.Result("", "")), \
                 mock.patch.object(dbs.apply_mod, "probe", return_value=({}, {}, {})), \
                 mock.patch.object(dbs.converge_run, "upload_blob", side_effect=_upload_blob), \
                 mock.patch.object(dbs.apply_mod, "apply", side_effect=_apply), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(self._Stop):
                    dbs.migrate(conf, "main-db", "dev")
        self.assertEqual([b.dest for b in uploaded], [layout.blob_path(sha)])   # one blob, by hash
        link = next(l for l in applied_links if l.path == f"{layout.ext_dir('main-db')}/ext.so")
        self.assertEqual(link.target, layout.blob_path(sha))                    # symlinked into ext dir
        self.assertTrue(any("! -name ext.so" in s for s in sudos))              # pruned to the listed set
