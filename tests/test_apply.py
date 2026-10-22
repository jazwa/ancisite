import re
import unittest

from anci.apply import FileArtifact, LinkArtifact, diff_files, diff_links
from anci.apply import ApplyResult


class TestDiffFiles(unittest.TestCase):
    def test_unchanged_skipped(self):
        f = FileArtifact(path="/u", content="hello", mode="0644")
        import hashlib

        h = hashlib.sha256(b"hello").hexdigest()
        self.assertEqual(diff_files([f], {"/u": h}), [])

    def test_changed_and_missing_uploaded(self):
        f = FileArtifact(path="/u", content="hello")
        g = FileArtifact(path="/v", content="world")
        result = diff_files([f, g], {"/u": "stale", "/v": None})
        self.assertEqual(result, [f, g])

    def test_bytes_content(self):
        f = FileArtifact(path="/bin", content=b"\x00\x01")
        import hashlib

        h = hashlib.sha256(b"\x00\x01").hexdigest()
        self.assertEqual(diff_files([f], {"/bin": h}), [])
        self.assertEqual(diff_files([f], {"/bin": None}), [f])


class TestDiffLinks(unittest.TestCase):
    def test_unchanged_skipped(self):
        l = LinkArtifact(path="/t", target="/var/lib/anci/exe/x/current")
        self.assertEqual(diff_links([l], {"/t": l.target}), [])

    def test_missing_and_wrong(self):
        l1 = LinkArtifact(path="/t", target="/cur")
        l2 = LinkArtifact(path="/u", target="/cur2")
        result = diff_links([l1, l2], {"/t": None, "/u": "/wrong"})
        self.assertEqual(result, [l1, l2])


class TestApplyResult(unittest.TestCase):
    def test_changed(self):
        self.assertFalse(ApplyResult().changed)
        self.assertTrue(ApplyResult(changed_files=["/x"]).changed)
        self.assertTrue(ApplyResult(changed_links=["/t"]).changed)


class TestShellSafety(unittest.TestCase):
    """Paths come from the box (an exe dir listing) — they must never act as shell."""

    HOSTILE = ["/var/lib/anci/exe/x; touch /tmp/pwned",
               "/var/lib/anci/exe/$(id)",
               "/var/lib/anci/exe/a b",
               "/var/lib/anci/exe/`id`"]

    def _scripts(self, **kw):
        import contextlib
        import io
        import subprocess
        from unittest import mock

        from anci import apply as apply_mod

        sudo = mock.MagicMock()
        with mock.patch.object(apply_mod.ssh, "sudo", sudo), \
             mock.patch.object(apply_mod.ssh, "put"), \
             mock.patch.object(apply_mod, "verify_upload"), \
             contextlib.redirect_stdout(io.StringIO()):
            apply_mod.apply(mock.Mock(), kw.get("files", []), kw.get("links", []),
                            probe_result=({}, {}, {p: True for p in self.HOSTILE}),
                            deletes=kw.get("deletes"))
        scripts = [c.args[1] for c in sudo.call_args_list]
        for s in scripts:   # every script must still be valid shell
            self.assertEqual(subprocess.run(["bash", "-n"], input=s, text=True).returncode, 0, s)
        return scripts

    def test_deletes_are_quoted(self):
        import shlex

        from anci.apply import DeleteArtifact
        scripts = self._scripts(deletes=[DeleteArtifact(p, tree=True) for p in self.HOSTILE])
        for p, s in zip(self.HOSTILE, scripts):
            self.assertEqual(s, f"rm -rf {shlex.quote(p)}")

    def test_files_and_links_are_quoted(self):
        import shlex
        p = "/etc/anci/units/x; reboot.service"
        scripts = self._scripts(files=[FileArtifact(path=p, content="x")],
                                links=[LinkArtifact(path="/etc/systemd/system/a b", target=p)])
        self.assertIn(shlex.quote(p), scripts[0])
        self.assertNotIn("; reboot", scripts[0].replace(shlex.quote(p), ""))
        self.assertRegex(scripts[1], r"^ln -sfn " + re.escape(shlex.quote(p))
                         + r" '/etc/systemd/system/a b\.anci-[0-9a-f]{8}' && mv -T "
                         r"'/etc/systemd/system/a b\.anci-[0-9a-f]{8}' "
                         + re.escape(shlex.quote('/etc/systemd/system/a b')) + "$")


class TestDeleteArtifactGuard(unittest.TestCase):
    def test_refuses_paths_anci_does_not_own(self):
        from anci.apply import DeleteArtifact
        for bad in ("/", "/etc", "/var/lib/anci", "/var/lib/anci/exe", "relative/x",
                    "/var/lib/anci/exe/../../etc", "/var/lib/anci/exe//x", "/etc/passwd",
                    "/var/lib/anci/exe/x/"):
            with self.assertRaises(ValueError, msg=bad):
                DeleteArtifact(bad, tree=True)

    def test_accepts_the_paths_anci_deletes(self):
        from anci import layout
        from anci.apply import DeleteArtifact
        for ok in (f"{layout.EXE_DIR}/gone", f"{layout.UNITS_DIR}/gone.service",
                   "/etc/systemd/system/gone.timer", layout.LITESTREAM_FILE, layout.CADDY_FILE):
            DeleteArtifact(ok)


if __name__ == "__main__":
    unittest.main()
