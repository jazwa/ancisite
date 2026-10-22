"""`dbs` — db dirs, extension files, db-file probe. Schema is not here."""

import tempfile
import unittest
from pathlib import Path

from anci.commands import dbs as dbs_mod
from anci import errors, layout
from anci.apply import sha256_file
from anci.converge import dbs

from .support import file_at, load_conf, make_ctx

CONF_EXT = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[exe.app]
source = './app'

[db.main-db]
migrations = './m'
extensions = ['./ext/uuid.so']
"""


class TestDbs(unittest.TestCase):
    def test_prepare_creates_both_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            c = dbs.contribute(make_ctx(conf))
            blob = "\n".join(c.prepare)
            self.assertIn(layout.db_dir("main-db"), blob)
            self.assertIn(layout.ext_dir("main-db"), blob)

    def test_db_file_is_never_hashed(self):
        # a db can be GBs: converge never reads it (migrate owns its content)
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            c = dbs.contribute(make_ctx(conf))
            self.assertNotIn(layout.db_path("main-db"), [f.path for f in c.files])
            self.assertFalse(hasattr(c, "probe"))

    def test_extension_ships_as_a_blob_and_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "ext").mkdir()
            p = Path(td) / "ext" / "uuid.so"
            p.write_bytes(b"\x7fELF")
            conf = load_conf(td, CONF_EXT)
            c = dbs.contribute(make_ctx(conf))
            sha = sha256_file(p)
        link = f"{layout.ext_dir('main-db')}/uuid.so"
        self.assertEqual([(b.dest, b.mode) for b in c.store_blobs],
                         [(layout.blob_path(sha), "0755")])          # a blob in the store
        self.assertEqual(next(l.target for l in c.links if l.path == link),
                         layout.blob_path(sha))                      # symlinked into ext dir
        self.assertEqual(c.files, [])                                # no longer a plain file
        self.assertIn("! -name uuid.so", "\n".join(c.prepare))       # prunes anything else

    def test_missing_extension_is_a_loud_error(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, CONF_EXT)   # ext/uuid.so never created
            with self.assertRaises(errors.AppError):
                dbs.contribute(make_ctx(conf))
