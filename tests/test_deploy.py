import struct
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from anci import box
from anci import auth as _auth, errors, net, paths
from anci.conf import ExeOverride
from anci.commands import deploy as dep
from anci.hashes import sha256_file
from anci.conf import Exe, load

CONF = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[env.prod]
ip = '203.0.113.10'
access-users = ['jack']
mode = 'adopt'
vm = { anci-version = '0.0.1' }

[exe.app]
source = './bin/server'

[exe.app.env.dev]

[exe.app.env.prod]
"""


@contextmanager
def conf_as(username: str):
    """A loaded conf with releases.identify_user returning `username`."""
    fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    fd.write(CONF)
    fd.close()
    conf = load(fd.name)
    with mock.patch.object(_auth, "identify_user", return_value=username):
        yield conf


class TestResolveEnvIp(unittest.TestCase):
    def test_remote_ip_passthrough(self):
        with conf_as("jack") as conf:
            self.assertEqual(box.resolve_env_ip(conf, "prod"), "203.0.113.10")

    def test_localhost_is_deterministic_vm_ip(self):
        # Static addressing: '127.0.0.1' resolves to the vm's assigned
        # address with no discovery, no root, no libvirt connection.
        with conf_as("jack") as conf:
            ip = box.resolve_env_ip(conf, "dev")
            self.assertRegex(ip, r"^192\.168\.122\.\d+$")
            self.assertEqual(ip, net.env_ip(paths.box_id(conf), "dev"))  # deterministic


class TestDeployGuards(unittest.TestCase):
    def test_unknown_exe_is_conf_class(self):
        with conf_as("jack") as conf, self.assertRaises(errors.ConfError) as cm:
            dep.deploy(conf, "nope", "dev")
        self.assertIn("isn't in anci.toml", str(cm.exception))

    def test_undefined_env_is_conf_class(self):
        # subtables are config overrides only; the only presence error left is
        # naming an env that doesn't exist
        with conf_as("jack") as conf, self.assertRaises(errors.ConfError) as cm:
            dep.deploy(conf, "app", "nope")
        self.assertIn("isn't in anci.toml", str(cm.exception))

    def test_missing_source_is_app_class(self):
        with conf_as("jack") as conf:
            conf.exes["app"].source = "/nonexistent/binary"
            with self.assertRaises(errors.AppError) as cm:
                dep.deploy(conf, "app", "dev")
            self.assertIn("doesn't exist", str(cm.exception))
            self.assertIn("build it first", cm.exception.try_)

    def test_key_not_in_conf_is_auth_class(self):
        with conf_as("?") as conf, self.assertRaises(errors.AuthError):
            dep.deploy(conf, "app", "dev")

    def test_unmatched_name_is_auth_class(self):
        # a name that isn't in the conf's users at all → unauthorized
        with conf_as("alice") as conf, self.assertRaises(errors.AuthError):
            dep.deploy(conf, "app", "prod")


class TestDeployShipsOnlyTheNamedExe(unittest.TestCase):
    def test_other_exes_keep_what_is_deployed(self):
        with conf_as("jack") as conf, tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "server"
            binary.write_bytes(b"\x7fELF" + bytes([2, 1]) + b"\x00" * 12 + (62).to_bytes(2, "little"))
            conf.exes["app"].source = str(binary)
            # a second exe whose local build doesn't exist: not this deploy's business
            conf.exes["worker"] = Exe(name="worker", source="/nonexistent/worker")
            with mock.patch.object(dep, "converge", return_value="ip") as c:
                dep.deploy(conf, "app", "dev")
        self.assertEqual(c.call_args.kwargs["inputs"]["exe"].binaries, {"app": binary})


class TestDeployAll(unittest.TestCase):
    """`deploy` with no exe: every exe, shipped in one converge."""

    ELF = b"\x7fELF" + bytes([2, 1]) + b"\x00" * 12 + (62).to_bytes(2, "little")

    def test_ships_every_exe_in_one_converge(self):
        with conf_as("jack") as conf, tempfile.TemporaryDirectory() as td:
            for name in ("app", "worker"):
                (Path(td) / name).write_bytes(self.ELF)
            conf.exes["app"].source = str(Path(td) / "app")
            conf.exes["worker"] = Exe(name="worker", source=str(Path(td) / "worker"))
            with mock.patch.object(dep, "converge", return_value="ip") as c:
                dep.deploy(conf, None, "dev")
        c.assert_called_once()
        self.assertEqual(c.call_args.kwargs["inputs"]["exe"].binaries,
                         {"app": Path(td) / "app", "worker": Path(td) / "worker"})
        self.assertEqual(set(c.call_args.kwargs["inputs"]["exe"].source), {"app", "worker"})

    def test_one_missing_build_ships_nothing(self):
        with conf_as("jack") as conf, tempfile.TemporaryDirectory() as td:
            (Path(td) / "app").write_bytes(self.ELF)
            conf.exes["app"].source = str(Path(td) / "app")
            conf.exes["worker"] = Exe(name="worker", source="/nonexistent/worker")
            with mock.patch.object(dep, "converge") as c, \
                 self.assertRaises(errors.AppError) as cm:
                dep.deploy(conf, None, "dev")
        self.assertIn("/nonexistent/worker", str(cm.exception))
        c.assert_not_called()

    def test_no_exes_is_conf_class(self):
        with conf_as("jack") as conf:
            conf.exes.clear()
            with self.assertRaises(errors.ConfError) as cm:
                dep.deploy(conf, None, "dev")
        self.assertIn("no exes", str(cm.exception))

class TestSha256(unittest.TestCase):
    def test_known_vector(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(b"hello")
            path = Path(f.name)
        self.assertEqual(
            sha256_file(path),
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        )


class TestUrl(unittest.TestCase):
    def test_local_url_is_the_localhost_forward(self):
        with conf_as("jack") as conf:
            name = box.localhost_name(conf)
            self.assertEqual(name.count("."), 1)   # <project>.localhost
            self.assertTrue(name.endswith(".localhost"))
            self.assertEqual(box.app_url(conf, "app", "dev", {"app": 20000}),
                             f"http://{name}:20000")
            # no forward installed yet: the VM's address
            self.assertEqual(box.app_url(conf, "app", "dev"),
                             f"http://{net.env_ip(paths.box_id(conf), 'dev')}:8000")

    def test_remote_url_is_the_host_or_internal(self):
        with conf_as("jack") as conf:
            self.assertEqual(box.app_url(conf, "app", "prod"), "http://127.0.0.1:8000")
            conf.exes["app"].env_overrides["prod"] = ExeOverride(env="prod", host="app.example.com")
            self.assertEqual(box.app_url(conf, "app", "prod"), "https://app.example.com")


class TestSshHint(unittest.TestCase):
    def test_authorized_user_gets_ssh_command(self):
        with conf_as("jack") as conf:
            # a local env resolves to its assignable address, like every command
            self.assertEqual(box.ssh_hint(conf, "dev", "jack"),
                             f"ssh jack@{net.env_ip(paths.box_id(conf), 'dev')}")

    def test_remote_env_uses_conf_ip(self):
        with conf_as("jack") as conf:
            self.assertEqual(box.ssh_hint(conf, "prod", "jack"),
                             "ssh jack@203.0.113.10")

    def test_unknown_key_gets_no_hint(self):
        with conf_as("?") as conf:
            self.assertIsNone(box.ssh_hint(conf, "dev", "?"))

    def test_user_without_access_gets_no_hint(self):
        with conf_as("jack") as conf:
            self.assertIsNone(box.ssh_hint(conf, "dev", "alice"))

    def test_unknown_env_gets_no_hint(self):
        with conf_as("jack") as conf:
            self.assertIsNone(box.ssh_hint(conf, "nope", "jack"))


def _elf(vm: int) -> bytes:
    """A minimal ELF header (magic, 64-bit LE, given e_vm)."""
    return b"\x7fELF" + bytes([2, 1]) + b"\x00" * 12 + struct.pack("<H", vm)


def _write(tmp: Path, data: bytes) -> Path:
    p = tmp / "app"
    p.write_bytes(data)
    return p


class TestBinaryArch(unittest.TestCase):
    def test_reads_elf_vm(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self.assertEqual(dep.binary_arch(_write(tmp, _elf(62))), "x86_64")
            self.assertEqual(dep.binary_arch(_write(tmp, _elf(183))), "arm64")
            self.assertEqual(dep.binary_arch(_write(tmp, _elf(3))), "x86 (32-bit)")

    def test_scripts_and_foreign_formats(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self.assertEqual(dep.binary_arch(_write(tmp, b"#!/bin/sh\n")), "script")
            self.assertEqual(dep.binary_arch(_write(tmp, b"MZ\x90\x00")), "pe")
            self.assertEqual(dep.binary_arch(_write(tmp, b"\xcf\xfa\xed\xfe\x00")), "mach-o")
            self.assertIsNone(dep.binary_arch(_write(tmp, b"hello")))

    def test_check_accepts_only_x86_64(self):
        with tempfile.TemporaryDirectory() as td:
            dep.check_app_arch(_write(Path(td), _elf(62)))   # x86_64: ok

    def test_check_rejects_scripts(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(errors.AppError) as cm:
                dep.check_app_arch(_write(Path(td), b"#!/bin/sh\n"))
            self.assertIn("script", str(cm.exception).lower())

    def test_check_rejects_the_rest(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with self.assertRaises(errors.AppError) as cm:
                dep.check_app_arch(_write(tmp, _elf(183)))   # arm64
            self.assertIn("arm64", str(cm.exception))
            with self.assertRaises(errors.AppError):
                dep.check_app_arch(_write(tmp, b"MZ\x90\x00"))        # windows
            with self.assertRaises(errors.AppError):
                dep.check_app_arch(_write(tmp, b"\xcf\xfa\xed\xfe\x00"))  # macos
            with self.assertRaises(errors.AppError):
                dep.check_app_arch(_write(tmp, b"hello"))             # not executable


class TestDeployArchGuard(unittest.TestCase):
    def test_rejects_wrong_arch_before_upload(self):
        # the guard runs before any ssh: an arm64 source fails as an app error
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "anci.toml"
            p.write_text(CONF)
            (Path(td) / "bin").mkdir()
            (Path(td) / "bin" / "server").write_bytes(_elf(183))
            conf = load(str(p))
            with mock.patch.object(_auth, "identify_user", return_value="jack"):
                with self.assertRaises(errors.AppError) as cm:
                    dep.deploy(conf, "app", "dev")
            self.assertIn("arm64", str(cm.exception))


class TestBindAndUrl(unittest.TestCase):
    def test_bind_address_rules(self):
        with conf_as("jack") as conf:
            # local: the localhost forward reaches the app at the VM's address
            self.assertEqual(box.bind_address(conf, "app", "dev"), "0.0.0.0")
            # remote: caddy fronts a host; without one the service is internal
            self.assertEqual(box.bind_address(conf, "app", "prod"), "127.0.0.1")

    def test_reachable_url(self):
        with conf_as("jack") as conf:
            self.assertEqual(
                box.reachable_url(conf, "app", "dev", {"app": 20000}),
                f"http://{box.localhost_name(conf)}:20000",
            )
            # remote + no host → nothing a user can open
            self.assertIsNone(box.reachable_url(conf, "app", "prod"))
            conf.exes["app"].env_overrides["prod"] = ExeOverride(env="prod", host="app.example.com")
            self.assertEqual(
                box.reachable_url(conf, "app", "prod"), "https://app.example.com"
            )


class TestAppLogs(unittest.TestCase):
    def _argv(self, *, follow: bool, log_exists: bool) -> list[str]:
        captured = {}
        with conf_as("jack") as conf, \
             mock.patch.object(dep.auth, "authorize", return_value="jack"), \
             mock.patch.object(dep.ssh, "connect", return_value=dep.ssh.Conn(ip="127.0.0.1")), \
             mock.patch.object(dep.ssh, "test", return_value=log_exists), \
             mock.patch.object(dep.ssh.Conn, "argv", side_effect=lambda args: captured.setdefault("args", args)), \
             mock.patch.object(dep.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            dep.app_logs(conf, "app", "dev", follow=follow)
        return captured["args"]

    def test_prefers_log_file(self):
        script = " ".join(self._argv(follow=False, log_exists=True))
        self.assertIn("/var/log/anci/app.log", script)
        self.assertNotIn("journalctl", script)

    def test_follows_log_file(self):
        self.assertIn("-F", " ".join(self._argv(follow=True, log_exists=True)))

    def test_a_short_log_continues_into_the_rotated_one(self):
        # just after the hourly trim: the newest lines span <log>.1 and <log>
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "app.log"
            Path(f"{log}.1").write_text("".join(f"old {i}\n" for i in range(100)))
            log.write_text("new 0\nnew 1\n")
            out = subprocess.run(["sh", "-c", dep.log_script(str(log), follow=False)],
                                 capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(len(out), 50)
        self.assertEqual((out[0], out[-2], out[-1]), ("old 52", "new 0", "new 1"))

    def test_falls_back_to_journal(self):
        args = self._argv(follow=False, log_exists=False)
        self.assertIn("journalctl", args)
        self.assertIn("app.service", args)



if __name__ == "__main__":
    unittest.main()
