"""`accounts` — the users/keys/sudoers + base layout root script."""

import tempfile
import unittest

from anci.converge import accounts

from .support import load_conf, make_ctx


class TestAccounts(unittest.TestCase):
    def _contribute(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            return accounts.contribute(make_ctx(conf))

    def test_one_contribution_named_accounts(self):
        c = self._contribute()
        self.assertEqual(c.name, "accounts")

    def test_prepare_covers_accounts_and_layout(self):
        c = self._contribute()
        blob = "\n".join(c.prepare)
        self.assertIn("groupadd anci", blob)                    # shared service group
        self.assertIn("useradd --system", blob)                 # shared service user
        self.assertIn("useradd --create-home --shell /bin/bash jack", blob)
        self.assertIn("jack ALL=(ALL) NOPASSWD:ALL", blob)      # passwordless sudo
        self.assertIn("ssh-ed25519 AAAA jack", blob)            # authorized key
        self.assertIn("/var/lib/anci", blob)                    # layout dirs

    def test_contributes_no_files_or_activations(self):
        c = self._contribute()
        self.assertEqual(c.files, [])
        self.assertEqual(c.links, [])
        self.assertEqual(c.after_apply, [])
