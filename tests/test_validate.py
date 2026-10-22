"""Cross-reference validation: `anci.validate`.

`validate_conf` (pure, run by `conf.load`) is covered by `test_conf`; these
tests target the secrets cross-reference that makes a misconfigured env fail
before any command runs.
"""

import tempfile
import unittest
from pathlib import Path

from anci import validate
from anci.conf import ConfError, load

CONF = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[exe.app]
source = './bin/server'

[replication]
bucket = 'my-bucket'
access-key-env = 'R2_ACCESS_KEY_ID'
secret-key-env = 'R2_SECRET_ACCESS_KEY'

[db.main-db]
migrations = './m'

[db.main-db.replication]
"""

NO_CREDS = CONF.replace(
    "access-key-env = 'R2_ACCESS_KEY_ID'\n", ""
).replace("secret-key-env = 'R2_SECRET_ACCESS_KEY'\n", "")


class TestValidateEnv(unittest.TestCase):
    def _conf(self, directory: str, text: str = CONF):
        p = Path(directory) / "anci.toml"
        p.write_text(text)
        return load(str(p))

    def test_named_creds_without_secrets_fails(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(td)
            with self.assertRaises(ConfError) as cm:
                validate.validate_env(conf, "dev")
            msg = str(cm.exception)
            self.assertIn("missing replication credentials", msg)
            self.assertIn("R2_ACCESS_KEY_ID", cm.exception.why)

    def test_plaintext_secrets_with_keys_pass(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(td)
            d = Path(td) / ".anci"
            d.mkdir()
            (d / "secrets.dev.env").write_text(
                "R2_ACCESS_KEY_ID=x\nR2_SECRET_ACCESS_KEY=y\n"
            )
            validate.validate_env(conf, "dev")  # must not raise

    def test_plaintext_secrets_missing_a_key_fails(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(td)
            d = Path(td) / ".anci"
            d.mkdir()
            (d / "secrets.dev.env").write_text("R2_ACCESS_KEY_ID=x\n")
            with self.assertRaises(ConfError):
                validate.validate_env(conf, "dev")

    def test_encrypted_secrets_presence_passes(self):
        # the encrypted tier can't be read offline; its presence is the check
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(td)
            d = Path(td) / "envs" / "dev"
            d.mkdir(parents=True)
            (d / "secrets.env").write_text("ENC[AES256_GCM,data:...]")
            validate.validate_env(conf, "dev")  # must not raise

    def test_default_chain_needs_no_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(td, NO_CREDS)
            validate.validate_env(conf, "dev")  # must not raise

    def test_unknown_env_is_a_noop(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(td)
            validate.validate_env(conf, "prod")  # not defined -> returns

    def test_load_does_not_require_secrets(self):
        # validate_env is a CLI-time check; loading a conf alone is fine
        with tempfile.TemporaryDirectory() as td:
            self._conf(td)


if __name__ == "__main__":
    unittest.main()
