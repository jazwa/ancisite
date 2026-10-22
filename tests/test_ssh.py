import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anci import ssh


class TestConnArgv(unittest.TestCase):
    def _conn(self):
        return ssh.Conn(ip="1.2.3.4", opts=["-o", "BatchMode=yes"])

    def test_simple(self):
        argv = self._conn().argv(["true"])
        self.assertEqual(argv[-1], "true")

    def test_script_is_quoted_for_remote_shell(self):
        # regression: ssh joins post-destination argv into one string for the
        # REMOTE shell; without quoting, "bash -c mkdir -p a b" lost its args.
        argv = self._conn().argv(["bash", "-c", "mkdir -p /a /b"])
        self.assertEqual(argv[-1], "bash -c 'mkdir -p /a /b'")

        # and the joined string parses back to the original argv remotely
        import shlex

        self.assertEqual(shlex.split(argv[-1]), ["bash", "-c", "mkdir -p /a /b"])

    def test_nasty_script_survives(self):
        script = "sudo -n bash -c 'install -m 0644 /tmp/x /etc/y && echo \"done\"'"
        argv = self._conn().argv(["bash", "-c", script])
        import shlex

        self.assertEqual(shlex.split(argv[-1]), ["bash", "-c", script])

    def test_pin_opts_flow_into_argv(self):
        conn = ssh.Conn(ip="x", opts=["-o", "UserKnownHostsFile=/repo/envs/prod/hostkey",
                                      "-o", "StrictHostKeyChecking=yes"])
        argv = conn.argv(["true"])
        self.assertIn("UserKnownHostsFile=/repo/envs/prod/hostkey", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)


class TestSSHError(unittest.TestCase):
    def test_unreachable_flag(self):
        e = ssh.SSHError("nope", unreachable=True)
        self.assertTrue(e.unreachable)
        self.assertFalse(ssh.SSHError("boom").unreachable)

    def test_auth_flag_defaults_off(self):
        self.assertFalse(ssh.SSHError("boom").auth)


class TestClassify(unittest.TestCase):
    def test_permission_denied_is_auth(self):
        self.assertEqual(ssh._classify("Permission denied (publickey)."), (False, True))

    def test_host_key_is_its_own_class(self):
        self.assertEqual(ssh._classify("Host key verification failed."), (True, False))

    def test_connection_refused_is_neither(self):
        self.assertEqual(
            ssh._classify("ssh: connect to host x port 22: Connection refused"), (False, False)
        )


class TestConnPort(unittest.TestCase):
    def test_default_port_is_22(self):
        self.assertEqual(ssh.Conn(ip="x").argv(["true"])[:3], ["ssh", "-p", "22"])

    def test_custom_port_flows_into_argv(self):
        self.assertIn("2222", ssh.Conn(ip="x", port=2222).argv(["true"]))

    def test_ssh_argv_for_transfer(self):
        argv = ssh.Conn(ip="x", port=2222).ssh_argv(["cat > /tmp/f"])
        self.assertEqual(argv[:3], ["ssh", "-p", "2222"])
        self.assertEqual(argv[-1], "cat > /tmp/f")


class TestRunClassification(unittest.TestCase):
    def _run(self, stderr: str):
        conn = ssh.Conn(ip="1.2.3.4")
        p = mock.Mock(returncode=255, stdout="", stderr=stderr)
        with mock.patch.object(ssh.subprocess, "run", return_value=p):
            with self.assertRaises(ssh.SSHError) as cm:
                ssh.run(conn, ["true"])
        return cm.exception

    def test_auth_failure(self):
        e = self._run("Permission denied (publickey).")
        self.assertTrue(e.auth)
        self.assertFalse(e.host_key)

    def test_host_key_failure(self):
        e = self._run("Host key verification failed.")
        self.assertTrue(e.host_key)
        self.assertFalse(e.auth)

    def test_unreachable(self):
        e = self._run("ssh: connect to host x port 22: No route to host")
        self.assertTrue(e.unreachable)
        self.assertFalse(e.auth)


class TestTransfersUseSsh(unittest.TestCase):
    """No scp / SFTP subsystem: both directions stream over plain ssh."""

    def test_put_streams_over_ssh(self):
        conn = ssh.Conn(ip="1.2.3.4")
        captured = {}

        class FakeStdin:
            def write(self, b): pass
            def close(self): pass

        class FakeStream:
            def read(self): return b""

        class FakePopen:
            def __init__(self, cmd, **kw):
                captured["cmd"] = cmd
                self.stdin = FakeStdin()
                self.stdout = FakeStream()
                self.stderr = FakeStream()

            def wait(self, timeout=None): return 0
            def kill(self): pass

        with tempfile.NamedTemporaryFile() as f, \
             mock.patch.object(ssh.subprocess, "Popen", FakePopen):
            ssh.put(conn, f.name, "/tmp/f")
        self.assertEqual(captured["cmd"][0], "ssh")
        self.assertNotIn("scp", captured["cmd"])
        self.assertEqual(captured["cmd"][-1], "cat > /tmp/f")

    def test_put_down_streams_over_ssh(self):
        conn = ssh.Conn(ip="1.2.3.4")
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return mock.Mock(returncode=0, stdout=b"bytes", stderr=b"")

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(ssh.subprocess, "run", side_effect=fake_run):
            out = Path(td) / "pulled"
            ssh.put_down(conn, "/tmp/f", str(out))
            self.assertEqual(out.read_bytes(), b"bytes")
        self.assertEqual(captured["cmd"][0], "ssh")
        self.assertNotIn("scp", captured["cmd"])
        self.assertEqual(captured["cmd"][-1], "cat /tmp/f")


class TestHelpersCompose(unittest.TestCase):
    """Regression: sudo()/test() once shadowed the module-level script()
    function with their `script` parameter — only visible at call time.
    Every remote helper must actually compose a command via run()."""

    def setUp(self):
        self.calls = []

        def fake_run(conn, args, *, timeout=120):
            self.calls.append((conn, args, timeout))
            return ssh.Result(stdout="", stderr="")

        self.patcher = unittest.mock.patch.object(ssh, "run", side_effect=fake_run)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.conn = ssh.Conn(ip="1.2.3.4")

    def test_script(self):
        ssh.script(self.conn, "mkdir -p /a", timeout=9)
        conn, args, timeout = self.calls[0]
        self.assertIs(conn, self.conn)
        self.assertEqual(timeout, 9)
        self.assertEqual(args, ["bash", "-c", "mkdir -p /a"])

    def test_sudo_wraps_in_sudo_bash_c(self):
        ssh.sudo(self.conn, "systemctl restart demo.service")
        _, args, _ = self.calls[0]
        self.assertEqual(args, ["bash", "-c", "sudo -n bash -c 'systemctl restart demo.service'"])

    def test_sudo_quotes_embedded_quotes(self):
        ssh.sudo(self.conn, "echo \"hi there\"")
        _, args, _ = self.calls[0]
        self.assertEqual(args, ["bash", "-c", "sudo -n bash -c 'echo \"hi there\"'"])

    def test_test_returns_bool(self):
        self.assertTrue(ssh.test(self.conn, "true"))
        _, args, _ = self.calls[0]
        self.assertEqual(args, ["bash", "-c", "true"])


class TestWaitReady(unittest.TestCase):
    def test_returns_immediately_when_cloud_init_done(self):
        conn = ssh.Conn(ip="x")
        calls = []

        def fake_script(c, s, **k):
            calls.append(s)
            if "cloud-init status" in s and "--wait" not in s:
                return ssh.Result("status: done", "")
            return ssh.Result("", "")

        with mock.patch.object(ssh, "run", return_value=ssh.Result("", "")), \
             mock.patch.object(ssh, "script", side_effect=fake_script):
            ssh.wait_ready(conn)
        self.assertFalse(any("--wait" in s for s in calls))   # didn't block

    def test_boot_finished_answers_before_cloud_init_status(self):
        calls = []
        with mock.patch.object(ssh, "run", return_value=ssh.Result("", "")), \
             mock.patch.object(ssh, "script",
                               side_effect=lambda c, s, **k: calls.append(s) or ssh.Result("status: done", "")):
            ssh.wait_ready(ssh.Conn(ip="x"))
        self.assertTrue(calls[0].startswith("[ -e /var/lib/cloud/instance/boot-finished ] && "
                                            "echo 'status: done' || cloud-init status"))

    def test_waits_when_cloud_init_running(self):
        conn = ssh.Conn(ip="x")
        calls = []

        def fake_script(c, s, **k):
            calls.append(s)
            if "--wait" in s:
                return ssh.Result("", "")
            return ssh.Result("status: running", "")

        with mock.patch.object(ssh, "run", return_value=ssh.Result("", "")), \
             mock.patch.object(ssh, "script", side_effect=fake_script), \
             contextlib.redirect_stdout(io.StringIO()):
            ssh.wait_ready(conn)
        self.assertTrue(any("cloud-init status --wait" in s for s in calls))

    def test_retries_auth_until_it_works(self):
        conn = ssh.Conn(ip="x")
        results = [ssh.SSHError("no", auth=True), ssh.Result("", "")]
        with mock.patch.object(ssh, "run", side_effect=results), \
             mock.patch.object(ssh, "script",
                               return_value=ssh.Result("status: done", "")), \
             mock.patch.object(ssh.time, "sleep"):
            ssh.wait_ready(conn)   # should not raise

    def test_raises_on_persistent_auth(self):
        conn = ssh.Conn(ip="x")
        with mock.patch.object(ssh, "run", side_effect=ssh.SSHError("no", auth=True)):
            with self.assertRaises(ssh.SSHError):
                ssh.wait_ready(conn, auth_timeout=0)

    def test_adopted_box_fails_fast(self):
        # an adopted box's accounts predate its pin: a refusal is never a boot race
        for err in (ssh.SSHError("no", auth=True), ssh.SSHError("down", unreachable=True)):
            with mock.patch.object(ssh, "run", side_effect=err) as run, \
                 mock.patch.object(ssh.time, "sleep") as sleep:
                with self.assertRaises(ssh.SSHError):
                    ssh.wait_ready(ssh.Conn(ip="x", boots=False))
            run.assert_called_once()
            sleep.assert_not_called()

    def test_boot_race(self):
        auth, down, other = (ssh.SSHError("e", auth=True), ssh.SSHError("e", unreachable=True),
                             ssh.SSHError("e"))
        booting = ssh.Conn(ip="x")
        self.assertTrue(ssh.boot_race(booting, auth))
        self.assertTrue(ssh.boot_race(booting, down))
        self.assertFalse(ssh.boot_race(booting, other))
        self.assertFalse(ssh.boot_race(ssh.Conn(ip="x", boots=False), auth))


class TestMux(unittest.TestCase):
    """One shared master connection per (user, box, port) — the per-command
    SSH process cost multiplier lives and dies with these options."""

    def _load_in(self, td: str):
        from anci.conf import load
        fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        fd.write(
            "[user.jack]\nssh-pub-keys = 'k'\n\n"
            "[env.dev]\nip = '127.0.0.1'\naccess-users = ['jack']\n"
            "vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }\n"
        )
        fd.close()
        return load(fd.name)

    def test_socket_dir_is_private_and_per_uid(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict("os.environ", {"XDG_RUNTIME_DIR": td}), \
             mock.patch.object(ssh.os, "geteuid", return_value=1234):
            path = ssh._mux_socket("jack", "192.168.122.9", 22)
            parent = Path(path).parent
            self.assertEqual(parent, Path(td) / "anci-1234")
            self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
        self.assertRegex(Path(path).name, r"^mux-[0-9a-f]{12}$")
        # one socket per (user, box, port)
        self.assertNotEqual(path, ssh._mux_socket("jack", "192.168.122.9", 2222))

    def test_socket_path_fits_macos_limit(self):
        # macOS: $TMPDIR is ~50 chars, sockets cap at 104 bytes, ssh adds a 17-char suffix
        base = "/var/folders/zz/zyxvpxvq6csfxvn_n0000000000000/T"
        with mock.patch.dict("os.environ", {"XDG_RUNTIME_DIR": base}), \
             mock.patch.object(ssh.Path, "mkdir"):
            path = ssh._mux_socket("a-rather-long-username", "2001:db8:ffff:ffff:ffff:ffff:ffff:fffe", 22)
        self.assertLess(len(path) + 17, 104)

    def test_connect_opts_in_all_branches(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict("os.environ", {"XDG_RUNTIME_DIR": td}):
            conn = ssh.connect(self._load_in(td), "dev", "jack")
            sock = ssh._mux_socket("jack", conn.ip, 22)
        self.assertIn("ControlMaster=auto", conn.opts)
        self.assertIn("ControlPersist=10m", conn.opts)
        self.assertIn(f"ControlPath={sock}", conn.opts)
        # local box has no pinning (check from the preexisting contract)
        self.assertIn("StrictHostKeyChecking=no", conn.opts)
        self.assertTrue(conn.boots)

    def test_adopted_box_doesnt_boot(self):
        from anci.conf import load
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict("os.environ", {"XDG_RUNTIME_DIR": td}):
            p = Path(td) / "anci.toml"
            p.write_text("[user.jack]\nssh-pub-keys = 'k'\n\n"
                         "[env.prod]\nip = '203.0.113.10'\naccess-users = ['jack']\n"
                         "mode = 'adopt'\nvm = { anci-version = '0.0.1' }\n")
            (Path(td) / "envs" / "prod").mkdir(parents=True)
            (Path(td) / "envs" / "prod" / "hostkey").write_text("203.0.113.10 ssh-ed25519 AAAA\n")
            conn = ssh.connect(load(str(p)), "prod", "jack")
        self.assertFalse(conn.boots)


if __name__ == "__main__":
    unittest.main()


class TestFacts(unittest.TestCase):
    """`ssh.facts`: every probe in one script, each output line tagged by key."""

    def _run(self, probes, preamble=""):
        import subprocess

        out = subprocess.run(["bash", "-c", ssh.facts_script(probes, preamble)],
                             capture_output=True, text=True).stdout
        return ssh.parse_facts(out)

    def test_tags_lines_and_drops_silent_probes(self):
        found = self._run({"a": "echo one", "silent": "true", "fails": "false; echo late",
                           "err": "echo oops >&2"})
        self.assertEqual(found, {"a": ["one"], "fails": ["late"]})

    def test_multi_line_values_keep_tabs_and_blanks(self):
        found = self._run({"m": "printf 'x\\ty\\n\\nz\\n'"})
        self.assertEqual(found["m"], ["x\ty", "", "z"])

    def test_keys_with_spaces_and_paths(self):
        found = self._run({"f /etc/x y": "echo 1"})
        self.assertEqual(ssh.fact(found, "f /etc/x y"), "1")
        self.assertIsNone(ssh.fact(found, "nope"))

    def test_preamble_functions_are_shared_and_silent(self):
        found = self._run({"a": "hi A", "b": "hi B"}, preamble="echo noise; hi() { echo \"hi $1\"; }")
        self.assertEqual(found, {"a": ["hi A"], "b": ["hi B"]})

    def test_tab_in_key_is_rejected(self):
        with self.assertRaises(ValueError):
            ssh.facts_script({"a\tb": "true"})


class TestPrivateUploads(unittest.TestCase):
    """Anything that may carry decrypted secrets is owner-only from creation."""

    def test_local_temp_is_0600(self):
        import os
        from anci import scratch
        p = scratch.write_private("t-private", "SECRET=1\n")
        try:
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
            self.assertEqual(p.read_text(), "SECRET=1\n")
        finally:
            p.unlink()

    def test_private_put_sets_umask_on_the_box(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as f, \
             mock.patch.object(ssh.subprocess, "Popen") as popen:
            proc = popen.return_value
            proc.wait.return_value = 0
            proc.stderr.read.return_value = b""
            ssh.put(ssh.Conn(ip="x"), f.name, "/tmp/t", private=True)
            ssh.put(ssh.Conn(ip="x"), f.name, "/tmp/u")
        private, public = (c.args[0][-1] for c in popen.call_args_list)
        self.assertTrue(private.startswith("umask 077; cat > "))
        self.assertTrue(public.startswith("cat > "))   # migrations stay readable by sqlite


class TestGuestSha(unittest.TestCase):
    def test_reads_as_root(self):
        # the secrets file is root-only (0600): an unprivileged read always "misses"
        from anci import apply as apply_mod
        with mock.patch.object(ssh, "facts", return_value={"f /etc/anci/secrets.env": ["ab" * 32]}) as f:
            self.assertEqual(apply_mod.guest_sha(ssh.Conn(ip="x"), "/etc/anci/secrets.env"), "ab" * 32)
        self.assertIn("sha256sum", f.call_args.args[1]["f /etc/anci/secrets.env"])
        self.assertTrue(f.call_args.kwargs["root"])     # the whole probe runs under one sudo
