"""Static files in the converge pass: planning (exes) and executing (run)."""

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anci import auth, layout, releases, ssh
from anci import apply as apply_mod
from anci import secrets as secrets_mod
from anci.apply import ApplyResult
from anci.converge import exes
from anci.converge import run as run_mod
from anci.staticfiles import Scanned

from .support import CONF, load_conf, make_ctx, write_source
from .support import link_at, file_at

STATIC_CONF = CONF.replace("source = './app'\n", "source = './app'\nstatic = './public'\n")
ID = "0123456789abcdef"


def scanned(td) -> Scanned:
    pub = Path(td) / "public"
    pub.mkdir(exist_ok=True)
    (pub / "a.txt").write_bytes(b"hi\n")
    sha = apply_mod.sha256_file(pub / "a.txt")
    return Scanned(id=ID, root=pub, n_files=1, n_bytes=3, entries=[("a.txt", sha)])



def _no_lock(*a, **kw):
    return contextlib.nullcontext()


class TestPlan(unittest.TestCase):
    def test_link_upload_and_env_var_when_shipped(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, STATIC_CONF)
            c = exes.contribute(make_ctx(conf, binaries={"app": write_source(td)},
                                         statics={"app": scanned(td)}))
        dest = layout.static_version("app", ID)
        self.assertEqual(link_at(c, layout.static_link("app")).target, dest)
        self.assertEqual([(s.exe_name, s.dest) for s in c.statics], [("app", dest)])
        self.assertEqual(c.statics[0].entries[0][0], "a.txt")   # ships the per-file hashes
        unit = file_at(c, f"{layout.UNITS_DIR}/app.service").content
        self.assertIn(f"Environment=ANCI_STATIC_DIR={layout.static_link('app')}", unit)
        self.assertEqual(c.deletes, [])

    def test_new_static_files_restart_the_service(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, STATIC_CONF)
            c = exes.contribute(make_ctx(conf, binaries={"app": write_source(td)},
                                         statics={"app": scanned(td)}))
        service = next(a for a in c.after_apply if a.label == "app (service)")
        self.assertIn(layout.static_link("app"), service.watches)
        with mock.patch.object(ssh, "sudo") as sudo:
            detail = service.run(None, ApplyResult(changed_links=[layout.static_link("app")]))
        self.assertEqual(detail, "new static files")
        self.assertIn("systemctl restart app.service", sudo.call_args.args[1])

    def test_reconcile_renders_the_env_var_but_leaves_the_link(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, STATIC_CONF)
            c = exes.contribute(make_ctx(conf, exe_dirs=["app"]))
        self.assertIn("ANCI_STATIC_DIR", file_at(c, f"{layout.UNITS_DIR}/app.service").content)
        self.assertNotIn(layout.static_link("app"), [l.path for l in c.links])
        self.assertEqual(c.statics, [])
        self.assertEqual(c.deletes, [])

    def test_static_removed_from_the_conf_unlinks_it(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            c = exes.contribute(make_ctx(conf, exe_dirs=["app"]))
        self.assertEqual([d.path for d in c.deletes], [layout.static_link("app")])
        self.assertNotIn("ANCI_STATIC_DIR", file_at(c, f"{layout.UNITS_DIR}/app.service").content)


class TestRun(unittest.TestCase):
    def _run(self, td, *, present=False, apply_result=None, blob_present=True):
        conf = load_conf(td, STATIC_CONF)
        src = write_source(td)
        st = scanned(td)
        blob_dest = layout.blob_path(st.entries[0][1])
        sudo, uploads = [], []

        def fake_upload_blob(conn, blob, have_size, exists=None):
            uploads.append(blob)
            if have_size is None and exists is not None:
                have_size = exists.get(blob.dest)
            return have_size is None            # absent on the box -> "uploaded"

        exists = {layout.static_version("app", ID): present}
        if blob_present:
            exists[blob_dest] = "3"
        with mock.patch.object(auth, "identify_user", return_value="jack"), \
             mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
             mock.patch.object(ssh, "script",
                               side_effect=lambda c, s, **k: ssh.Result("done\n", "")), \
             mock.patch.object(ssh, "sudo", side_effect=lambda c, s, **kw: sudo.append(s) or ssh.Result("", "")), \
             mock.patch.object(ssh, "put"), \
             mock.patch.object(run_mod, "upload_blob", side_effect=fake_upload_blob), \
             mock.patch.object(secrets_mod, "resolve_secrets", return_value=(None, "")), \
             mock.patch.object(run_mod.forward, "reconcile", return_value={}), \
             mock.patch.object(apply_mod, "probe", return_value=({}, {}, exists)) as probe, \
             mock.patch.object(apply_mod, "apply", return_value=apply_result or ApplyResult()), \
             mock.patch.object(releases, "append") as append, \
             mock.patch.object(run_mod, "_gc"), \
                 mock.patch.object(run_mod.boxlock, "hold", new=_no_lock), \
             contextlib.redirect_stdout(io.StringIO()):
            run_mod.converge(conf, "dev", binaries={"app": src}, statics={"app": st})
        return {"sudo": sudo, "uploads": uploads, "probe": probe, "append": append,
                "blob_dest": blob_dest}

    def test_version_dir_and_its_blobs_are_probed(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._run(td)
        deletes = r["probe"].call_args.kwargs["deletes"]
        self.assertIn(layout.static_version("app", ID), deletes)
        self.assertIn(r["blob_dest"], deletes)

    def test_builds_a_symlink_tree_then_installs_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._run(td)
        build = next(s for s in r["sudo"] if "mv -T" in s)
        self.assertIn(f"ln -s {r['blob_dest']} ", build)      # file -> its blob
        self.assertIn(f"/app/.static-tmp-", build)
        self.assertIn(f"{layout.static_version('app', ID)}", build)   # atomic rename into place
        self.assertEqual(subprocess.run(["bash", "-n"], input=build, text=True).returncode, 0)

    def test_missing_blob_is_uploaded(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._run(td, blob_present=False)
        blob = next(b for b in r["uploads"] if b.dest == r["blob_dest"])
        self.assertEqual(blob.mode, "0644")              # static files aren't executable

    def test_present_version_is_touched_not_built(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._run(td, present=True)
        self.assertIn(f"touch {layout.static_version('app', ID)}", r["sudo"])
        self.assertFalse(any("mv -T" in s or "ln -s" in s for s in r["sudo"]))

    def test_static_only_change_is_a_release(self):
        with tempfile.TemporaryDirectory() as td:
            same = self._run(td)
            moved = self._run(td, apply_result=ApplyResult(
                changed_links=[layout.static_link("app")]))
        same["append"].assert_not_called()
        moved["append"].assert_called_once()
        self.assertEqual(moved["append"].call_args.kwargs["static"], ID)


if __name__ == "__main__":
    unittest.main()
