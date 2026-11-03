"""`anci <env> up` dispatch: one verb for every kind of box (no box)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anci import cli, errors
from anci.conf import load

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
        with mock.patch("anci.providers.libvirt.env_up") as vm, \
             mock.patch("anci.inspect.run.converge") as infra:
            cli._run_env(self.conf, env, list(argv), debug=False)
        return vm, infra

    def test_up_creates_the_vm_then_converges(self):
        for env in ("dev", "prod"):
            with self.subTest(env=env):
                vm, infra = self.run_env(env, "up")
                vm.assert_called_once()
                infra.assert_called_once_with(self.conf, env, grow_disk=True, pipe=mock.ANY)
                # one report: creating the VM and the converge share a pipeline
                self.assertIs(vm.call_args.kwargs["pipe"], infra.call_args.kwargs["pipe"])

    def test_local_vm_on_macos_errors_before_libvirt(self):
        with mock.patch.object(cli.sys, "platform", "darwin"), \
             self.assertRaises(errors.ConfError) as cm:
            self.run_env("dev", "up")
        self.assertIn("needs Linux", str(cm.exception))

    def test_remote_host_vm_on_macos_works(self):
        with mock.patch.object(cli.sys, "platform", "darwin"):
            vm, _ = self.run_env("prod", "up")
        vm.assert_called_once()


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
             mock.patch("anci.inspect.run.converge") as infra, \
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
             mock.patch("anci.inspect.run.converge", side_effect=lambda *a, **k: order.append("converge")):
            cli._run_env(self.conf, "dev", ["reconcile"], debug=False)
        self.assertEqual(order, ["vm", "recipients", "converge"])
        sync.assert_called_once_with(self.conf, "dev")

    def test_reconcile_stops_when_the_secrets_cannot_be_re_encrypted(self):
        with mock.patch("anci.providers.libvirt.vm_reconcile"), \
             mock.patch("anci.secrets.sync_recipients", side_effect=errors.AuthError("no")), \
             mock.patch("anci.inspect.run.converge") as infra:
            with self.assertRaises(errors.AuthError):
                cli._run_env(self.conf, "dev", ["reconcile"], debug=False)
        infra.assert_not_called()

    def test_reconcile_takes_no_arguments(self):
        with self.assertRaises(errors.ConfError):
            cli._run_env(self.conf, "dev", ["reconcile", "app"], debug=False)
