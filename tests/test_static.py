"""Static files: conf, packing/version ids, gc, deploy."""

import contextlib
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from anci import auth, errors, layout, staticfiles
from anci.commands import deploy as dep
from anci.conf import ConfError, load
from anci.inspect import walk

CONF = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIdd5266e9506c16ebf7e12ffa7545d4c41feb0b2322d jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[env.prod]
ip = '203.0.113.10'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[exe.app]
source = './bin/server'
static = './public'

[exe.app.env.prod]
static = './public-prod'
"""

ELF = b"\x7fELF" + bytes([2, 1]) + b"\x00" * 12 + (62).to_bytes(2, "little")


def load_in(td, text=CONF):
    p = Path(td) / "anci.toml"
    p.write_text(text)
    return load(str(p))


def tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


class TestConf(unittest.TestCase):
    def test_static_and_its_override(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_in(td)
        self.assertEqual(conf.exes["app"].resolve_for("dev").static, "./public")
        self.assertEqual(conf.exes["app"].resolve_for("prod").static, "./public-prod")

    def test_optional(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_in(td, CONF.replace("static = './public'\n", "")
                           .replace("[exe.app.env.prod]\nstatic = './public-prod'\n", ""))
        self.assertIsNone(conf.exes["app"].resolve_for("dev").static)

    def test_bad_type(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ConfError) as cm:
                load_in(td, CONF.replace("static = './public'", "static = 3"))
        self.assertIn("'static' must be a directory path", str(cm.exception))

    def test_allowed_on_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_in(td, CONF + "\n[exe.job]\nsource = './j'\nschedule = 'daily'\n"
                                      "static = './data'\n")
        self.assertEqual(conf.exes["job"].resolve_for("dev").static, "./data")


class TestScan(unittest.TestCase):
    FILES = {"index.html": b"<h1>hi</h1>", "css/site.css": b"body{}", "img/a.png": b"\x89PNG"}

    def test_same_files_same_id_whatever_mtimes_order_and_modes(self):
        with tempfile.TemporaryDirectory() as td:
            a = tree(Path(td) / "a", self.FILES)
            b = tree(Path(td) / "b", dict(reversed(list(self.FILES.items()))))
            os.utime(b / "index.html", (1, 1))
            os.chmod(b / "css/site.css", 0o755)
            sa, sb = staticfiles.scan(a), staticfiles.scan(b)
        self.assertEqual(sa.id, sb.id)
        self.assertTrue(layout.is_static_id(sa.id))
        self.assertEqual((sa.n_files, sa.n_bytes), (3, sum(map(len, self.FILES.values()))))

    def test_id_changes_with_content_or_path(self):
        with tempfile.TemporaryDirectory() as td:
            base = staticfiles.scan(tree(Path(td) / "a", self.FILES)).id
            content = staticfiles.scan(tree(Path(td) / "b", {**self.FILES, "index.html": b"x"})).id
            moved = dict(self.FILES)
            moved["home.html"] = moved.pop("index.html")
            path = staticfiles.scan(tree(Path(td) / "c", moved)).id
        self.assertEqual(len({base, content, path}), 3)

    def test_empty_dirs_are_not_shipped(self):
        with tempfile.TemporaryDirectory() as td:
            a = tree(Path(td) / "a", self.FILES)
            b = tree(Path(td) / "b", self.FILES)
            (b / "empty").mkdir()
            self.assertEqual(staticfiles.scan(a).id, staticfiles.scan(b).id)

    def test_refuses_symlinks_fifos_and_odd_names(self):
        with tempfile.TemporaryDirectory() as td:
            link = tree(Path(td) / "l", self.FILES)
            (link / "out").symlink_to("/etc")
            fifo = tree(Path(td) / "f", self.FILES)
            os.mkfifo(fifo / "pipe")
            nl = tree(Path(td) / "n", {"a\nb": b"x"})
            for root, what in ((link, "symlink"), (fifo, "regular file"), (nl, "newline")):
                with self.assertRaises(errors.AppError, msg=what) as cm:
                    staticfiles.scan(root)
                self.assertIn(what, str(cm.exception))

    def test_bad_name(self):
        for bad in ("/abs", "../x", "a/../b", "a//b", "./a", "a\nb"):
            self.assertIsNotNone(staticfiles.bad_name(bad), bad)
        self.assertIsNone(staticfiles.bad_name("css/site.css"))


class TestGc(unittest.TestCase):
    def _gc(self, root: Path) -> None:
        """GC as a run has it: a step of the script, under `set -e`."""
        with mock.patch.object(layout, "EXE_DIR", str(root / "exe")), \
             mock.patch.object(layout, "BLOB_DIR", str(root / "blobs")), \
             mock.patch.object(layout, "EXT_DIR", str(root / "ext")), \
             mock.patch.object(layout, "SQLITE_BIN", str(root / "bin" / "sqlite3")):
            script = walk.script([walk._gc()])
        subprocess.run(["bash", "-c", script], check=True, capture_output=True)

    def test_keeps_the_target_and_young_dirs_removes_the_rest(self):
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "exe" / "app"
            old = time.time() - 3600
            for name in ("static-aaaa", "static-bbbb", ".static-tmp", "static-young"):
                (app / name).mkdir(parents=True)
                if name != "static-young":
                    os.utime(app / name, (old, old))
            (app / "static").symlink_to(app / "static-aaaa")
            self._gc(Path(td))
            self.assertEqual(sorted(p.name for p in app.iterdir()),
                             ["static", "static-aaaa", "static-young"])

    def test_keeps_linked_and_young_blobs_removes_the_rest(self):
        with tempfile.TemporaryDirectory() as td:
            blobs, app = Path(td) / "blobs", Path(td) / "exe" / "app"
            blobs.mkdir()
            app.mkdir(parents=True)
            old = time.time() - 3600
            for name in ("current", "static-file", "orphan", "young"):
                (blobs / name).write_text(name)
                if name != "young":
                    os.utime(blobs / name, (old, old))
            (app / "current").symlink_to(blobs / "current")
            (app / "static-v").mkdir()
            (app / "static-v" / "a.css").symlink_to(blobs / "static-file")
            (app / "static").symlink_to(app / "static-v")
            self._gc(Path(td))
            self.assertEqual(sorted(p.name for p in blobs.iterdir()),
                             ["current", "static-file", "young"])

    def test_with_no_exe_deployed_a_vendored_binary_is_kept(self):
        # no exe dir matches the globs; the reference listing must still finish
        with tempfile.TemporaryDirectory() as td:
            blobs = Path(td) / "blobs"
            blobs.mkdir()
            (Path(td) / "bin").mkdir()
            old = time.time() - 3600
            for name in ("sqlite", "orphan"):
                (blobs / name).write_text(name)
                os.utime(blobs / name, (old, old))
            (Path(td) / "bin" / "sqlite3").symlink_to(blobs / "sqlite")
            self._gc(Path(td))
            self.assertEqual(sorted(p.name for p in blobs.iterdir()), ["sqlite"])


@contextlib.contextmanager
def project(text=CONF):
    with tempfile.TemporaryDirectory() as td, \
         mock.patch.object(auth, "identify_user", return_value="jack"):
        (Path(td) / "bin").mkdir()
        (Path(td) / "bin" / "server").write_bytes(ELF)
        yield Path(td), load_in(td, text)


class TestDeploy(unittest.TestCase):
    def test_missing_dir_is_an_app_error(self):
        with project() as (td, conf), self.assertRaises(errors.AppError) as cm:
            dep.deploy(conf, "app", "dev")
        self.assertIn("isn't a directory", str(cm.exception))

    def test_scans_and_passes_statics(self):
        with project() as (td, conf), \
             mock.patch.object(dep, "converge", return_value="ip") as c:
            tree(td / "public", {"index.html": b"hi"})
            dep.deploy(conf, "app", "dev")
        scanned = c.call_args.kwargs["inputs"]["exe"].statics["app"]
        self.assertTrue(layout.is_static_id(scanned.id))
        self.assertEqual(scanned.root, td / "public")

    def test_no_static_passes_none(self):
        with project(CONF.replace("static = './public'\n", "")) as (td, conf), \
             mock.patch.object(dep, "converge", return_value="ip") as c:
            dep.deploy(conf, "app", "dev")
        self.assertEqual(c.call_args.kwargs["inputs"]["exe"].statics, {})


if __name__ == "__main__":
    unittest.main()
