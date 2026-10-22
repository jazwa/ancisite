"""`secrets` — courier the env's resolved secrets file."""

import tempfile
import unittest

from anci import layout
from anci.commands import dbs as dbs_mod
from anci.converge import secrets

from .support import file_at, load_conf, make_ctx


class TestSecrets(unittest.TestCase):
    def test_no_secrets_contributes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            c = secrets.contribute(make_ctx(conf, secrets=(None, "")))
            self.assertEqual(c.files, [])

    def test_content_is_couriered_mode_600(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            c = secrets.contribute(make_ctx(conf, secrets=("A=1\nB=2\n", "plaintext")))
            f = file_at(c, layout.SECRETS_FILE)
            self.assertEqual(f.content, "A=1\nB=2\n")
            self.assertEqual(f.mode, "0600")
