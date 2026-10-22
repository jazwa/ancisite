"""`vendored` — sqlite3, litestream and caddy binaries on every box, used or
not, so their upload lands on the box's first converge (`up`)."""

import tempfile
import unittest

from anci import layout
from anci.converge import vendored

from .support import load_conf, make_ctx

CONF_REMOTE = """
anci-version = '0.0.1'

[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.prod]
ip = '203.0.113.10'
mode = 'adopt'
access-users = ['jack']

[exe.app]
source = './app'

[db.main-db]
migrations = './m'
"""


class TestVendored(unittest.TestCase):
    def test_remote_env_gets_all_without_host_or_replication(self):
        with tempfile.TemporaryDirectory() as td:
            c = vendored.contribute(make_ctx(load_conf(td, CONF_REMOTE), env_name="prod"))
        self.assertEqual([d.dest for d in c.downloads],
                         [layout.LITESTREAM_BIN, layout.SQLITE_BIN, layout.CADDY_BIN])
        self.assertTrue(all(d.sha256 or d.sha512 for d in c.downloads))   # verified downloads

    def test_local_env_gets_no_caddy(self):
        # a local env never has a `host`, but still needs sqlite3 + litestream
        with tempfile.TemporaryDirectory() as td:
            c = vendored.contribute(make_ctx(load_conf(td)))
        self.assertEqual([d.dest for d in c.downloads],
                         [layout.LITESTREAM_BIN, layout.SQLITE_BIN])

    def test_nothing_to_start(self):
        # the litestream/caddy reconcilers own units and activation
        with tempfile.TemporaryDirectory() as td:
            c = vendored.contribute(make_ctx(load_conf(td, CONF_REMOTE), env_name="prod"))
        self.assertEqual((c.files, c.links, c.after_apply), ([], [], []))


if __name__ == "__main__":
    unittest.main()
