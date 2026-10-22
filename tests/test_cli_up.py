"""`anci <env> up` dispatch: one verb for every kind of box (no box)."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anci import cli, errors
from anci.conf import load

CONF = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[env.prod]
ip = '203.0.113.10'
mode = 'adopt'
access-users = ['jack']
vm = { anci-version = '0.0.1' }
"""


class TestUpDispatch(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        p = Path(self.td.name) / "anci.toml"
        p.write_text(CONF)
        self.conf = load(str(p))

    def tearDown(self):
        self.td.cleanup()

    def run_env(self, env, *argv):
        with mock.patch("anci.providers.adopt.up_adopt") as ext, \
             mock.patch("anci.providers.libvirt.env_up") as vm, \
             mock.patch("anci.converge.converge") as infra:
            cli._run_env(self.conf, env, list(argv), debug=False)
        return ext, vm, infra

    def test_adopt_up_bootstraps_then_converges(self):
        ext, vm, infra = self.run_env("prod", "up", "--user", "ubuntu")
        ext.assert_called_once_with(self.conf, "prod", login_user="ubuntu")
        vm.assert_not_called()
        infra.assert_called_once_with(self.conf, "prod", grow_disk=True)

    def test_adopt_up_without_user_passes_none(self):
        # up_adopt picks root on first contact, and skips the bootstrap on a set-up box
        ext, _, _ = self.run_env("prod", "up")
        self.assertIsNone(ext.call_args.kwargs["login_user"])

    def test_vm_up_goes_to_libvirt(self):
        ext, vm, infra = self.run_env("dev", "up")
        ext.assert_not_called()
        vm.assert_called_once()
        infra.assert_called_once()

    def test_declined_bootstrap_never_converges(self):
        with mock.patch("anci.providers.adopt.up_adopt",
                        side_effect=errors.EnvError("declined")), \
             mock.patch("anci.converge.converge") as infra:
            with self.assertRaises(errors.EnvError):
                cli._run_env(self.conf, "prod", ["up"], debug=False)
        infra.assert_not_called()

    def test_user_on_a_vm_env_errors(self):
        with self.assertRaises(errors.ConfError) as cm:
            self.run_env("dev", "up", "--user", "ubuntu")
        self.assertIn("--user", str(cm.exception))

    def test_recreate_on_adopt_errors(self):
        with self.assertRaises(errors.ConfError) as cm:
            self.run_env("prod", "up", "--recreate")
        self.assertIn("can't recreate", str(cm.exception))

    def test_adopt_is_gone(self):
        with self.assertRaises(errors.ConfError):
            cli._run_env(self.conf, "prod", ["adopt"], debug=False)

    def test_local_vm_on_macos_errors_before_libvirt(self):
        with mock.patch.object(cli.sys, "platform", "darwin"), \
             self.assertRaises(errors.ConfError) as cm:
            _, vm, _ = self.run_env("dev", "up")
        self.assertIn("needs Linux", str(cm.exception))

    def test_adopted_box_on_macos_works(self):
        with mock.patch.object(cli.sys, "platform", "darwin"):
            ext, _, _ = self.run_env("prod", "up")
        ext.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class TestReconcileDispatch(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        p = Path(self.td.name) / "anci.toml"
        p.write_text(CONF)
        self.conf = load(str(p))
        self.addCleanup(self.td.cleanup)

    def test_reconcile_only_converges(self):
        with mock.patch("anci.providers.libvirt.vm_reconcile") as vm, \
             mock.patch("anci.converge.converge") as infra, \
             mock.patch("anci.providers.libvirt.env_up") as up, \
             mock.patch("anci.commands.deploy.deploy") as deploy, \
             mock.patch("anci.commands.dbs.migrate") as migrate:
            cli._run_env(self.conf, "dev", ["reconcile"], debug=False)
        vm.assert_called_once_with(self.conf, "dev")
        infra.assert_called_once_with(self.conf, "dev", grow_disk=True)   # no binaries, no sources
        for never in (up, deploy, migrate):
            never.assert_not_called()

    def test_migrate_checks_running_and_never_resizes(self):
        with mock.patch("anci.providers.libvirt.require_running") as running, \
             mock.patch("anci.providers.libvirt.vm_reconcile") as vm, \
             mock.patch("anci.commands.dbs.migrate") as migrate:
            cli._run_env(self.conf, "dev", ["migrate", "main-db"], debug=False)
        running.assert_called_once_with(self.conf, "dev")
        vm.assert_not_called()
        migrate.assert_called_once()

    def test_reconcile_syncs_secret_recipients_before_converging(self):
        order = []
        with mock.patch("anci.providers.libvirt.vm_reconcile", side_effect=lambda *a: order.append("vm")), \
             mock.patch("anci.secrets.sync_recipients", side_effect=lambda *a, **k: order.append("recipients")) as sync, \
             mock.patch("anci.converge.converge", side_effect=lambda *a, **k: order.append("converge")):
            cli._run_env(self.conf, "dev", ["reconcile"], debug=False)
        self.assertEqual(order, ["vm", "recipients", "converge"])
        sync.assert_called_once_with(self.conf, "dev")

    def test_reconcile_stops_when_the_secrets_cannot_be_re_encrypted(self):
        with mock.patch("anci.providers.libvirt.vm_reconcile"), \
             mock.patch("anci.secrets.sync_recipients", side_effect=errors.AuthError("no")), \
             mock.patch("anci.converge.converge") as infra:
            with self.assertRaises(errors.AuthError):
                cli._run_env(self.conf, "dev", ["reconcile"], debug=False)
        infra.assert_not_called()

    def test_reconcile_takes_no_arguments(self):
        with self.assertRaises(errors.ConfError):
            cli._run_env(self.conf, "dev", ["reconcile", "app"], debug=False)
