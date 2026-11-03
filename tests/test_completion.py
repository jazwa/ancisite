"""Shell completion — Click's, driven by the commands in anci/cli.py, with values
(envs, exes, dbs) read from the conf on every TAB."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from click.shell_completion import ShellComplete

from anci import cli

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
mode = 'adopt'
vm = { anci-version = '0.0.1' }

[exe.web]
source = './bin/web'

[exe.job]
source = './bin/job'
schedule = 'daily'

[db.main-db]
migrations = './m'
"""


class TestComplete(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        Path(self.td.name, "anci.toml").write_text(CONF)
        self.old = os.getcwd()
        os.chdir(self.td.name)

    def tearDown(self):
        os.chdir(self.old)
        self.td.cleanup()

    def c(self, *words):
        *args, incomplete = words
        comp = ShellComplete(cli.root, {}, "anci", "_ANCI_COMPLETE")
        return [i.value for i in comp.get_completions(list(args), incomplete)]

    def test_top_level_offers_commands_and_envs(self):
        self.assertTrue({"init", "completion", "dev", "prod"} <= set(self.c("")))

    def test_env_offers_its_verbs(self):
        self.assertTrue({"up", "deploy", "migrate", "regen", "secrets"} <= set(self.c("dev", "")))

    def test_values_come_from_the_conf(self):
        self.assertEqual(sorted(self.c("dev", "deploy", "")), ["job", "web"])
        self.assertEqual(self.c("dev", "info", "db", ""), ["main-db"])
        self.assertEqual(self.c("dev", "migrate", ""), ["main-db"])

    def test_no_conf_offers_only_commands(self):
        os.chdir(self.old)
        with tempfile.TemporaryDirectory() as empty:
            os.chdir(empty)
            got = self.c("")
        self.assertIn("init", got)
        self.assertNotIn("dev", got)


class TestScript(unittest.TestCase):
    def test_every_shell_gets_a_script(self):
        for shell in ("bash", "zsh", "fish"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["completion", shell]), 0)
            self.assertIn("_ANCI_COMPLETE", out.getvalue())


class TestReservedEnvNames(unittest.TestCase):
    def test_every_top_level_command_is_reserved(self):
        # an env named like one would be unreachable: `anci <word>` runs the command
        from anci.validate import RESERVED_NAMES
        self.assertLessEqual(set(cli.root.commands), RESERVED_NAMES)


if __name__ == "__main__":
    unittest.main()
