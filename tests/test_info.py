"""`anci <env> info` — broad strokes: what it covers, how it sizes things, and that
it renders. Labels and layout are free to change without touching these."""

import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anci import auth, errors, layout, ssh
from anci.commands import info as info_mod
from anci.conf import Conf, Db, Env, Exe, ExeOverride, Replication, User


def make_conf() -> Conf:
    return Conf(
        users={"jack": User(name="jack", ssh_pub_keys=["k"])},
        envs={"dev": Env(name="dev", ip="127.0.0.1", access_users=["jack"],
                         anci_version="0.0.1")},
        exes={
            "app": Exe(name="app", source="./s", static="./public",
                       env_overrides={"dev": ExeOverride(env="dev")}),
            "job": Exe(name="job", source="./s",
                       schedule="daily", env_overrides={"dev": ExeOverride(env="dev")}),
        },
        dbs={"main-db": Db(name="main-db", migrations="./m/", extensions=["./ext/vec0.so"],
                           replication=Replication(bucket="backups"))},
    )


def _paths(sub) -> set[str]:
    return {p for p, _ in sub.candidates}


class TestCoverage(unittest.TestCase):
    """Which files each view covers."""

    def test_full_view_shares_env_files_once(self):
        subs = info_mod._subjects(make_conf(), "dev", None, None)
        self.assertEqual([(s.kind, s.name) for s in subs],
                         [("env", "dev"), ("exe", "app"), ("exe", "job"), ("db", "main-db")])
        self.assertIn(layout.SECRETS_FILE, _paths(subs[0]))
        self.assertIn(layout.LITESTREAM_FILE, _paths(subs[0]))
        for s in subs[1:]:
            self.assertNotIn(layout.SECRETS_FILE, _paths(s))

    def test_one_exe_or_db_includes_what_it_uses(self):
        conf = make_conf()
        app = info_mod._subjects(conf, "dev", "exe", "app")[0]
        self.assertTrue({layout.current_link("app"), layout.static_link("app"),
                         layout.SECRETS_FILE} <= _paths(app))
        db = info_mod._subjects(conf, "dev", "db", "main-db")[0]
        self.assertTrue({layout.db_path("main-db"), f"{layout.ext_dir('main-db')}/vec0.so",
                         layout.LITESTREAM_FILE} <= _paths(db))

    def test_bad_arguments_are_conf_errors(self):
        for kind, name in (("exe", None), ("exe", "nope"), ("box", "x"), (None, "app")):
            with self.assertRaises(errors.ConfError):
                info_mod._subjects(make_conf(), "dev", kind, name)


@unittest.skipUnless(shutil.which("find") and subprocess.run(
    ["find", "--version"], capture_output=True).returncode == 0, "needs GNU find (the box's)")
class TestDescribe(unittest.TestCase):
    """The probe's shell, run for real: static versions and ext dirs are trees of
    symlinks into the blob store, sized by what they point at."""

    def test_symlink_tree_counts_its_targets(self):
        with tempfile.TemporaryDirectory() as td:
            blob = Path(td) / "blob"
            blob.write_bytes(b"x" * 1000)
            tree = Path(td) / "static-1"
            (tree / "sub").mkdir(parents=True)
            (tree / "a.css").symlink_to(blob)
            (tree / "sub" / "b.js").symlink_to(blob)
            out = subprocess.run(["bash", "-c", info_mod._DESCRIBE + f"describe {tree}"],
                                 capture_output=True, text=True).stdout
        self.assertEqual(info_mod._stat(out.splitlines()[0]),
                         info_mod._Stat("dir", size=2000, files=2))


class TestInfo(unittest.TestCase):
    """The whole command over a mocked probe."""

    def _info(self, res, **kw) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
             mock.patch.object(auth, "authorize", return_value="jack"), \
             mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
             mock.patch.object(info_mod, "_probe", return_value=res):
            info_mod.info(make_conf(), "dev", **kw)
        return buf.getvalue()

    def test_renders_every_section_and_marks_a_shared_blob(self):
        blob = f"{layout.BLOB_DIR}/abcd1234abcd1234"
        res = {layout.current_link(e): info_mod._Stat("link", target=blob, resolved=blob)
               for e in ("app", "job")}
        res[blob] = info_mod._Stat("file", size=1024)
        out = self._info(res)
        self.assertIn("ssh jack@", out)
        for name in ("app", "job", "main-db"):
            self.assertIn(name, out)
        self.assertIn("shared with app", out)

    def test_unauthorized_leaks_nothing(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
             mock.patch.object(auth, "authorize", side_effect=errors.AuthError("no access")), \
             self.assertRaises(errors.AuthError):
            info_mod.info(make_conf(), "dev")
        self.assertNotIn("ssh", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
