"""`converge` — the executor: inventory, prepare, upload, apply, after_apply.

Transport-mocked (no box). One test per behavior that used to live in the
deploy pipeline tests.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anci import auth
from anci import apply as apply_mod
from anci import secrets as secrets_mod
from anci.commands import dbs
from anci import errors, layout, releases, ssh
from anci.apply import ApplyResult
from anci.converge import run as run_mod, vendored
from anci.converge.context import DesiredState

from .support import load_conf, write_source



def _no_lock(*a, **kw):
    return contextlib.nullcontext()


def _no_downloads():
    # the vendored binaries (litestream/sqlite3/caddy) ship on every converge;
    # suppress them where a test is about something else
    return mock.patch.object(vendored, "contribute", return_value=DesiredState(name="vendored"))


class TestInventory(unittest.TestCase):
    def _inventory(self, found):
        with mock.patch.object(ssh, "facts", return_value=found) as f:
            got = run_mod._inventory(ssh.Conn(ip="127.0.0.1"))
        return got, f.call_args.args[1]

    def test_reads_ci_units_and_exe_dirs(self):
        got, probes = self._inventory({"ci": ["status: done"],
                                       "unit": ["a.service", "b.timer"], "exe": ["app"]})
        self.assertEqual(got, ("status: done", {"a.service", "b.timer"}, ["app"]))
        self.assertIn("cloud-init status", probes["ci"])

    def test_empty_is_not_a_boot_claim(self):
        got, _ = self._inventory({})
        self.assertEqual(got, ("", set(), []))

    HOSTILE = ["x; touch /tmp/pwned", "$(id)", "a b", "`id`", "..", ".hidden", "-rf"]

    def test_gather_drops_names_anci_could_not_have_created(self):
        answers = [("status: done", {"app.service", "evil;reboot.service", "a b.timer"},
                    ["app", *self.HOSTILE])]
        with mock.patch.object(run_mod, "_inventory", side_effect=answers):
            units, dirs, rejected = run_mod._gather(None)
        self.assertEqual((units, dirs), ({"app.service"}, ["app"]))
        self.assertEqual(sorted(rejected), sorted(
            [f"{layout.EXE_DIR}/{n}" for n in self.HOSTILE]
            + [f"{layout.UNITS_DIR}/evil;reboot.service", f"{layout.UNITS_DIR}/a b.timer"]))

    def test_boot_check_skips_cloud_init_once_booted(self):
        with mock.patch.object(ssh, "facts", return_value={}) as facts:
            run_mod._inventory(ssh.Conn(ip="x"))
        ci = facts.call_args.args[1]["ci"]
        self.assertTrue(ci.startswith("[ -e /var/lib/cloud/instance/boot-finished ] && echo 'status: done'"))
        self.assertIn("|| cloud-init status", ci)     # only while boot-finished is missing

    def test_mid_boot_waits_then_rereads(self):
        answers = [("status: running", set(), []), ("status: done", {"a.service"}, ["app"])]
        with mock.patch.object(run_mod, "_inventory", side_effect=answers), \
             mock.patch.object(ssh, "wait_ready") as wait:
            self.assertEqual(run_mod._gather(None), ({"a.service"}, ["app"], []))
        wait.assert_called_once()


def _sha(path):
    return apply_mod.sha256_file(path)


class TestConverge(unittest.TestCase):
    """End-to-end converge with a fully mocked transport."""

    def _run(self, conf, src, *, probe=None, apply_result=None, script=None, statics=None):
        captured = {"sudo": []}

        def _default_script(conn, text, *, timeout=120):
            if "sha256sum" in text:
                return ssh.Result(f"{_sha(src)}  /tmp/x\n", "")
            return ssh.Result("CI status: done\n", "")

        def _sudo(conn, text, *, timeout=120):
            captured["sudo"].append(text)
            return ssh.Result("", "")

        out = io.StringIO()
        with _no_downloads(), \
             mock.patch.object(auth, "identify_user", return_value="jack"), \
             mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
             mock.patch.object(ssh, "wait_ready"), \
             mock.patch.object(ssh, "script", side_effect=script or _default_script), \
             mock.patch.object(ssh, "sudo", side_effect=_sudo), \
             mock.patch.object(ssh, "put") as put, \
             mock.patch.object(secrets_mod, "resolve_secrets", return_value=(None, "")), \
             mock.patch.object(run_mod.forward, "reconcile", return_value={}), \
             mock.patch.object(apply_mod, "probe",
                               return_value=probe or ({}, {}, {})) as probe_mock, \
             mock.patch.object(apply_mod, "apply",
                               return_value=apply_result or ApplyResult()), \
             mock.patch.object(releases, "append") as append, \
             mock.patch.object(run_mod, "_gc"), \
                 mock.patch.object(run_mod.boxlock, "hold", new=_no_lock), \
             contextlib.redirect_stdout(out):
            run_mod.converge(conf, "dev", binaries={"app": src}, statics=statics)
        captured["out"] = out.getvalue()
        captured["put"], captured["probe"] = put, probe_mock
        captured["append"] = append
        return captured

    def test_prepare_carries_accounts_and_layout(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            captured = self._run(conf, src)
        blob = "\n".join(captured["sudo"])
        self.assertIn("usermod -aG anci", blob)          # accounts
        self.assertIn("touch /var/log/anci/app.log", blob)  # per-exe log

    def test_release_logged_only_when_current_changes(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            same = self._run(conf, src)   # `current` already pointed here
            moved = self._run(conf, src, apply_result=ApplyResult(
                changed_links=[layout.current_link("app")]))
        same["append"].assert_not_called()
        moved["append"].assert_called_once()
        self.assertEqual(moved["append"].call_args.args[1], "app")

    def test_rejected_entries_are_warned_about_after_the_step_and_never_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            bad = f"{layout.EXE_DIR}/x; touch /tmp/pwned"
            with mock.patch.object(run_mod, "_gather",
                                   return_value=({"app.service"}, ["app"], [bad])):
                res = self._run(conf, src)
        self.assertIn("ignored", res["out"])
        self.assertIn("touch /tmp/pwned", res["out"])
        self.assertFalse(any("touch /tmp/pwned" in s and "rm" in s for s in res["sudo"]))

    def test_removed_exe_leftovers_are_reported(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            kept = "/var/log/anci/ghost.log"
            with mock.patch.object(run_mod, "_gather",
                                   return_value=({"app.service", "ghost.service"}, ["app", "ghost"], [])), \
                 mock.patch("anci.converge.orphans._existing", return_value=[kept]):
                out = self._run(conf, src)["out"]
        self.assertIn("stopped ghost.service (no longer in anci.toml)", out)
        self.assertIn(f"kept {kept} (from a removed exe)", out)

    def test_waits_out_auth_lag_then_retries(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            state = {"n": 0}

            def _script(conn, text, *, timeout=120):
                if "sha256sum" in text:
                    return ssh.Result(f"{_sha(src)}  /tmp/x\n", "")
                state["n"] += 1
                if state["n"] == 1:
                    raise ssh.SSHError("Permission denied (publickey)", auth=True)
                return ssh.Result("CI status: done\n", "")

            with _no_downloads(), \
                 mock.patch.object(auth, "identify_user", return_value="jack"), \
                 mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
                 mock.patch.object(ssh, "wait_ready") as wait_ready, \
                 mock.patch.object(ssh, "script", side_effect=_script), \
                 mock.patch.object(ssh, "sudo", return_value=ssh.Result("", "")), \
                 mock.patch.object(ssh, "put"), \
                 mock.patch.object(secrets_mod, "resolve_secrets", return_value=(None, "")), \
                 mock.patch.object(run_mod.forward, "reconcile", return_value={}), \
                 mock.patch.object(apply_mod, "probe", return_value=({}, {}, {})), \
                 mock.patch.object(apply_mod, "apply", return_value=ApplyResult()), \
                 mock.patch.object(releases, "append"), \
                 mock.patch.object(run_mod, "_gc"), \
                 mock.patch.object(run_mod.boxlock, "hold", new=_no_lock), \
                 contextlib.redirect_stdout(io.StringIO()):
                run_mod.converge(conf, "dev", binaries={"app": src})
        wait_ready.assert_called_once()

    def test_vendored_binaries_ship_as_blobs_and_symlinks(self):
        # a local env ships litestream + sqlite3 (no caddy); each is a blob in the
        # store, with its bin path a symlink into it
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            fake = Path(td) / "vend"
            fake.write_bytes(b"VENDORED-BINARY")
            bsha = _sha(fake)
            applied, sudos = {}, []

            def _apply(conn, files, links, probe_result=None, deletes=None):
                applied["links"] = {l.path: l.target for l in links}
                return ApplyResult()

            def _script(conn, text, *, timeout=120):
                return ssh.Result(f"{bsha}  x\n" if "sha256sum" in text else "CI status: done\n", "")

            with mock.patch.object(auth, "identify_user", return_value="jack"), \
                 mock.patch.object(run_mod.bin_mod, "vendor", return_value=fake), \
                 mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
                 mock.patch.object(ssh, "wait_ready"), \
                 mock.patch.object(ssh, "script", side_effect=_script), \
                 mock.patch.object(ssh, "sudo", side_effect=lambda c, t, **k: sudos.append(t) or ssh.Result("", "")), \
                 mock.patch.object(ssh, "put"), \
                 mock.patch.object(secrets_mod, "resolve_secrets", return_value=(None, "")), \
                 mock.patch.object(run_mod.forward, "reconcile", return_value={}), \
                 mock.patch.object(apply_mod, "probe",   # app binary present; the vendored blob is not
                                   return_value=({}, {}, {layout.blob_path(_sha(src)): str(src.stat().st_size)})), \
                 mock.patch.object(apply_mod, "apply", side_effect=_apply), \
                 mock.patch.object(releases, "append"), \
                 mock.patch.object(run_mod, "_gc"), \
                 mock.patch.object(run_mod.boxlock, "hold", new=_no_lock), \
                 contextlib.redirect_stdout(io.StringIO()):
                run_mod.converge(conf, "dev", binaries={"app": src})
        self.assertEqual(applied["links"].get(layout.LITESTREAM_BIN), layout.blob_path(bsha))
        self.assertEqual(applied["links"].get(layout.SQLITE_BIN), layout.blob_path(bsha))
        self.assertNotIn(layout.CADDY_BIN, applied["links"])   # local env: no caddy
        self.assertTrue(any("mv -f" in s and layout.blob_path(bsha) in s for s in sudos))

    def test_blob_name_collision_is_a_loud_error(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            bpath = layout.blob_path(_sha(src))
            with self.assertRaises(errors.EnvError) as cm:
                self._run(conf, src, probe=({}, {}, {bpath: "1"}))   # same name, other size
        self.assertIn("collides", str(cm.exception))

    def test_existing_blob_is_judged_by_size_not_rehashed(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            bpath = layout.blob_path(_sha(src))
            have = self._run(conf, src, probe=({}, {}, {bpath: str(src.stat().st_size)}))
            missing = self._run(conf, src)
        self.assertFalse(any(c.args[2].startswith(layout.BLOB_DIR) for c in have["put"].call_args_list))
        self.assertTrue(any(c.args[2].startswith(layout.BLOB_DIR) for c in missing["put"].call_args_list))
        self.assertNotIn(bpath, have["probe"].call_args.args[1])         # not in the hashed files
        self.assertIn(bpath, have["probe"].call_args.kwargs["deletes"])   # only stat'ed

    def test_local_binary_is_hashed_once(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            have = {layout.blob_path(_sha(src)): str(src.stat().st_size)}   # no upload to verify
            with mock.patch.object(apply_mod, "sha256_file", wraps=apply_mod.sha256_file) as h:
                self._run(conf, src, probe=({}, {}, have), apply_result=ApplyResult(
                    changed_links=[layout.current_link("app")]))   # plan + release
        self.assertEqual([c.args[0] for c in h.call_args_list], [src])

    def test_noop_reports_nothing_to_restart(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            out = self._run(conf, src)["out"]
        self.assertIn("applying", out)
        self.assertIn("unchanged", out)
        self.assertIn("nothing to restart", out)
        self.assertNotIn("activating", out)  # the old, misleading label

    def test_change_reports_what_restarted(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            changed = ApplyResult(changed_links=[f"{layout.EXE_DIR}/app/current"])
            out = self._run(conf, src, apply_result=changed)["out"]
        self.assertIn("new version", out)
        self.assertNotIn("nothing to restart", out)

