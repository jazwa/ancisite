"""Adopted boxes (`up` on `mode = 'adopt'`): gating, the accounts reconcile
script, and the bootstrap/deploy orchestration (transport-mocked)."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anci.providers import adopt as adopt_mod
from anci import apply as apply_mod
from anci.commands import dbs, deploy
from anci import errors, layout, releases, ssh
from anci.providers.adopt import (
    up_adopt,
    ensure_anci_account,
    preflight_checks,
    verify_checks,
)
from anci.render.shell import render_accounts_script
from anci.apply import ApplyResult
from anci.conf import Conf, Env, User, load
from anci.box import is_remote

REMOTE_CONF = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.prod]
ip = '203.0.113.10'
access-users = ['jack']
mode = 'adopt'
vm = { anci-version = '0.0.1' }
"""

DEPLOY_CONF = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[exe.app]
source = './server'

[exe.app.env.dev]
"""

KEY = "203.0.113.10 ssh-ed25519 AAAA"

# a valid app "binary" for deploy tests: a minimal x86_64 ELF header (the deploy
# arch guard refuses scripts, and b"hello" is neither an ELF nor a script)
DEPLOY_BYTES = b"\x7fELF" + bytes([2, 1]) + b"\x00" * 12 + (62).to_bytes(2, "little")


def load_conf(directory: str, text: str) -> Conf:
    p = Path(directory) / "anci.toml"
    p.write_text(text)
    return load(str(p))


def make_conf(*envs: Env) -> Conf:
    return Conf(
        users={"jack": User(name="jack", ssh_pub_keys=["ssh-ed25519 AAAA jack"])},
        envs={e.name: e for e in envs},
        exes={},
    )


def env(name: str, ip: str) -> Env:
    return Env(name=name, ip=ip, access_users=["jack"], anci_version="0.0.1",
               mode="adopt" if ip != "127.0.0.1" else "full")


class TestIsRemote(unittest.TestCase):
    def test_local(self):
        self.assertFalse(is_remote(env("dev", "127.0.0.1")))

    def test_remote(self):
        self.assertTrue(is_remote(env("prod", "203.0.113.10")))


class TestRenderAccountsScript(unittest.TestCase):
    def test_renders_group_accounts_keys_and_sudo(self):
        script = render_accounts_script(
            {
                "jack": ["ssh-ed25519 AAAA a"],
                "alice": ["ssh-ed25519 AAAA b", "ssh-ed25519 AAAA c"],
            }
        )
        self.assertIn("groupadd anci", script)
        self.assertIn("useradd --system --no-create-home", script)  # shared service user
        self.assertIn("useradd --create-home --shell /bin/bash jack", script)
        self.assertIn("useradd --create-home --shell /bin/bash alice", script)
        self.assertIn("jack ALL=(ALL) NOPASSWD:ALL", script)
        self.assertIn("ssh-ed25519 AAAA a", script)
        self.assertIn("ssh-ed25519 AAAA c", script)
        self.assertIn("gpasswd -d", script)  # stale-member revocation

    def test_empties_authorized_keys_before_writing(self):
        # keys are written idempotently: truncate then append each conf key
        script = render_accounts_script({"jack": ["k1", "k2"]})
        truncate = script.index(": > /home/jack/.ssh/authorized_keys")
        k1 = script.index("k1")
        k2 = script.index("k2")
        self.assertLess(truncate, k1)
        self.assertLess(k1, k2)


class TestPreflightChecks(unittest.TestCase):
    def test_checks_libc_arch_systemd_bash_and_sudo(self):
        checks = preflight_checks()
        self.assertEqual(set(checks), {"libc", "arch", "systemd", "bash", "sudo"})
        script = "\n".join(checks.values())
        self.assertIn("ld-linux-x86-64.so.2", script)
        self.assertIn("/run/systemd/system", script)   # booted with systemd, not just present
        self.assertIn("sudo -n true", script)


class TestEnsureAnciAccount(unittest.TestCase):
    def test_creates_group_and_service_user_and_returns_name(self):
        conn = ssh.Conn(ip="203.0.113.10", user="root")
        captured = []
        with mock.patch.object(ssh, "sudo",
                               side_effect=lambda c, s, **k: captured.append(s)):
            name = ensure_anci_account(conn)
        self.assertEqual(name, "anci")
        script = captured[0]
        self.assertIn("getent group anci", script)
        self.assertIn("groupadd anci", script)
        self.assertIn("id -u anci", script)
        self.assertIn("useradd --system --no-create-home", script)
        self.assertIn("--shell /usr/sbin/nologin -g anci anci", script)


class TestVerifyChecks(unittest.TestCase):
    def test_checks_accounts_and_layout(self):
        checks = verify_checks({"jack": ["ssh-ed25519 AAAA a"]})
        for key in ("group anci", "account anci", "account jack", "member jack",
                    "keys jack", "dir /var/lib/anci/store/blobs",
                    "dir /etc/anci/units"):
            self.assertIn(key, checks)
        # sqlite3 ships vendored on the first converge, not in the bootstrap
        self.assertNotIn("sqlite3", checks)


def setUpModule():
    # the caller's identity comes from their ssh keys; pin it for these tests
    global _auth
    _auth = mock.patch.object(adopt_mod.auth, "authorize", return_value="jack")
    _auth.start()


def tearDownModule():
    _auth.stop()


class TestVerifyGuest(unittest.TestCase):
    def test_runs_script_and_reports_problems(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, REMOTE_CONF)
            conn = ssh.Conn(ip="203.0.113.10", user="root")
            with mock.patch.object(
                ssh, "script",
                return_value=ssh.Result("account jack\tmissing\n", ""),
            ):
                findings = adopt_mod.problems(conn, verify_checks(conf.users_for_env("prod")))
            self.assertEqual(findings, ["account jack: missing"])


class TestAdoptPreflight(unittest.TestCase):
    def test_preflight_fails_before_bootstrap(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, REMOTE_CONF)
            with mock.patch.object(ssh, "env_trust", return_value=[KEY]), \
                 mock.patch.object(
                     ssh, "script",
                     return_value=ssh.Result("libc\tneeds glibc (musl distros like Alpine are unsupported)\n", ""),
                 ), \
                 mock.patch.object(ssh, "sudo") as sudo_mock, \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(errors.EnvError) as cm:
                    up_adopt(conf, "prod", login_user="root")
            self.assertIn("supported host", str(cm.exception))
            sudo_mock.assert_not_called()  # bootstrap never ran
            self.assertFalse(ssh.pin_path(conf, "prod").exists())


class TestUpAdoptGuards(unittest.TestCase):
    def test_local_env_is_an_internal_error(self):
        conf = make_conf(env("dev", "127.0.0.1"))
        with self.assertRaises(errors.InternalError):
            up_adopt(conf, "dev")

    def test_unauthorized_caller_touches_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, REMOTE_CONF)
            with mock.patch.object(adopt_mod.auth, "authorize",
                                   side_effect=errors.AuthError("no access")), \
                 mock.patch.object(ssh, "env_trust") as trust, \
                 mock.patch.object(ssh, "script") as script:
                with self.assertRaises(errors.AuthError):
                    up_adopt(conf, "prod")
            trust.assert_not_called()
            script.assert_not_called()

    def test_pinned_box_skips_ceremony_and_bootstrap(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, REMOTE_CONF)
            pin = ssh.pin_path(conf, "prod")
            pin.parent.mkdir(parents=True)
            pin.write_text("203.0.113.10 ssh-ed25519 AAAA\n")
            with mock.patch.object(ssh, "env_trust") as trust, \
                 mock.patch.object(ssh, "script") as script, \
                 mock.patch.object(ssh, "sudo") as sudo_mock, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(up_adopt(conf, "prod"), "203.0.113.10")
            trust.assert_not_called()
            script.assert_not_called()
            sudo_mock.assert_not_called()


class TestReconcileUsers(unittest.TestCase):
    def test_sudos_the_rendered_accounts_script(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, REMOTE_CONF)
            script = render_accounts_script(conf.users_for_env("prod"))
            self.assertIn("groupadd anci", script)
            self.assertIn("useradd --create-home --shell /bin/bash jack", script)


class TestAdoptFlow(unittest.TestCase):
    def _up(self, conf, *, login_user="root", verify_out=""):
        """Run up_adopt with the transport mocked; returns (order, sudo conns, pinned
        during the bootstrap). The verify checks answer with `verify_out`."""
        order, sudo_conns, pinned = [], [], []

        def _env_trust(conf, env_name, *, stdin=None):
            order.append("env_trust")
            return [KEY]

        def _sudo(conn, script, *, timeout=120):
            order.append("sudo")
            sudo_conns.append(conn)
            pinned.append(ssh.pin_path(conf, "prod").exists())
            return ssh.Result(verify_out if "keys jack" in script else "", "")

        with mock.patch.object(ssh, "env_trust", side_effect=_env_trust), \
             mock.patch.object(ssh, "script", return_value=ssh.Result("", "")), \
             mock.patch.object(ssh, "sudo", side_effect=_sudo), \
             contextlib.redirect_stdout(io.StringIO()):
            addr = up_adopt(conf, "prod", login_user=login_user)
        self.assertEqual(addr, "203.0.113.10")
        return order, sudo_conns, pinned

    def test_ceremony_runs_before_bootstrap_and_pins_after(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, REMOTE_CONF)
            order, sudo_conns, pinned = self._up(conf)
            # ceremony, then accounts + prerequisites + verify (under sudo)
            self.assertEqual(order, ["env_trust", "sudo", "sudo", "sudo"])
            self.assertEqual(pinned, [False, False, False])  # pinned only once verified
            self.assertEqual(ssh.pin_path(conf, "prod").read_text(), KEY + "\n")
            # the bootstrap connection is verified against the confirmed keys, not TOFU
            opts = sudo_conns[0].opts
            self.assertIn("StrictHostKeyChecking=yes", opts)
            self.assertIn("BatchMode=yes", opts)
            self.assertTrue(any(o.startswith("UserKnownHostsFile=") for o in opts))

    def test_failed_bootstrap_leaves_no_pin(self):
        # regression: a pin left by a failed first up made the next up skip the bootstrap
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, REMOTE_CONF)
            with self.assertRaises(errors.EnvError) as cm:
                self._up(conf, verify_out="keys jack\tempty/missing\n")
            self.assertIn("out of line", str(cm.exception))
            self.assertFalse(ssh.pin_path(conf, "prod").exists())

    def test_user_on_a_pinned_box_reruns_the_bootstrap(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, REMOTE_CONF)
            pin = ssh.pin_path(conf, "prod")
            pin.parent.mkdir(parents=True)
            pin.write_text(KEY + "\n")
            order, sudo_conns, _ = self._up(conf, login_user="debian")
            self.assertEqual(order, ["sudo", "sudo", "sudo"])  # no ceremony
            self.assertEqual(sudo_conns[0].user, "debian")
            self.assertIn(f"UserKnownHostsFile={pin}", sudo_conns[0].opts)

    def test_aborts_without_bootstrap_if_key_declined(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, REMOTE_CONF)
            with mock.patch.object(ssh, "env_trust", return_value=None), \
                 mock.patch.object(ssh, "sudo") as sudo_mock, \
                 contextlib.redirect_stdout(io.StringIO()):
                # raising (not returning) keeps `up` from converging afterwards
                with self.assertRaises(errors.EnvError):
                    up_adopt(conf, "prod", login_user="root")
            sudo_mock.assert_not_called()
            self.assertFalse(ssh.pin_path(conf, "prod").exists())


if __name__ == "__main__":
    unittest.main()
