"""The db resource, with its real probes run against a box laid out in a temp dir."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anci import layout
from anci.hashes import sha256_file
from anci.inspect import Action, ObservedState, TargetState, diff
from anci.inspect.db import Db

from .support import CONF, load_conf
from .support import link, run_probes, write

EXT = CONF.replace("migrations = './m'\n", "migrations = './m'\nextensions = ['./ext/uuid.so']\n")
REPLICATED = CONF + "\n[db.main-db.replication]\nbucket = 'backups'\n"


class TestDb(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = td.name
        for patch in (mock.patch.object(layout, "DB_DIR", f"{td.name}/db"),
                      mock.patch.object(layout, "EXT_DIR", f"{td.name}/ext")):
            patch.start()
            self.addCleanup(patch.stop)
        write(f"{td.name}/ext/uuid.so", "ext bytes")
        Path(layout.db_dir("main-db")).mkdir(parents=True)

    def _diff(self, conf):
        r = Db()
        return diff(ObservedState(r.observed(run_probes(r))), TargetState(r.target(conf, "dev")))

    def _link_ext(self):
        blob = layout.blob_id(sha256_file(Path(f"{self.root}/ext/uuid.so")))
        write(f"{self.root}/blobs/{blob}", "ext bytes")    # in the store: the link resolves
        link(f"{layout.ext_dir('main-db')}/uuid.so", f"{self.root}/blobs/{blob}")

    def test_matching_box_has_no_changes(self):
        self._link_ext()
        self.assertEqual(self._diff(load_conf(self.root, EXT)), [])

    def test_an_extension_dropped_from_the_conf_is_reported(self):
        self._link_ext()
        [change] = self._diff(load_conf(self.root))
        self.assertEqual(list(change.fields), ["exts"])
        self.assertEqual(change.fields["exts"][1], {})

    def test_a_replica_that_moved(self):
        write(layout.replica_record("main-db"), " backups old/main-db\n")
        [change] = self._diff(load_conf(self.root, REPLICATED))
        self.assertEqual(change.fields, {"replica": (" backups old/main-db", " backups dev/main-db")})

    def test_replication_turned_off_leaves_its_record(self):
        write(layout.replica_record("main-db"), " backups dev/main-db\n")
        [change] = self._diff(load_conf(self.root))
        self.assertEqual(change.fields, {"replica": (" backups dev/main-db", None)})

    def test_a_db_gone_from_the_conf_is_a_delete(self):
        Path(layout.db_dir("old")).mkdir()
        [change] = self._diff(load_conf(self.root))
        self.assertEqual((change.identifier, change.action), ("db.old", Action.DELETE))


if __name__ == "__main__":
    unittest.main()
