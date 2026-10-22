"""`disk` — grow the guest root fs to fill the block device (local only)."""

import tempfile
import unittest

from anci.converge import disk

from .support import CONF, load_conf, make_ctx


class TestDiskReconciler(unittest.TestCase):
    def test_local_env_grows_the_root_filesystem(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            ctx = make_ctx(conf)
            ctx.grow_disk = True                 # up / reconcile
            c = disk.contribute(ctx)
        self.assertEqual(c.name, "disk")
        self.assertEqual(len(c.prepare), 1)
        self.assertIn("growpart", c.prepare[0])
        self.assertIn("resize2fs", c.prepare[0])

    def test_deploy_and_migrate_skip_it(self):
        with tempfile.TemporaryDirectory() as td:
            c = disk.contribute(make_ctx(load_conf(td)))   # grow_disk defaults off
        self.assertEqual(c.prepare, [])

    def test_remote_env_is_untouched(self):
        adopted = CONF.replace(
            "ip = '127.0.0.1'", "ip = '203.0.113.10'\nmode = 'adopt'",
        ).replace(
            "vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }",
            "vm = { anci-version = '0.0.1' }",
        )
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, adopted)
            ctx = make_ctx(conf)
            ctx.grow_disk = True
            c = disk.contribute(ctx)
        self.assertEqual(c.prepare, [])
