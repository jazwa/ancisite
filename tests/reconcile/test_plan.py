"""`plan_env` — aggregation across every reconciler."""

import tempfile
import unittest

from anci import layout
from anci.converge import RECONCILERS, plan_env

from .support import CONF, load_conf, make_ctx, write_source

FULL = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '10.0.0.5'          # remote: caddy fronts the host
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[exe.app]
source = './app'
host = 'app.example.com'

[replication]
bucket = 'backups'

[db.main-db]
migrations = './m'

[db.main-db.replication]
"""


class TestPlan(unittest.TestCase):
    def test_contributions_follow_reconciler_order(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            plan = plan_env(make_ctx(conf, binaries={"app": src},
                                     present_units=set(), exe_dirs=[]))
            self.assertEqual(
                [c.name for c in plan.desired_states],
                [r.__name__.rsplit(".", 1)[-1] for r in RECONCILERS])

    def test_full_conf_aggregates_every_concern(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, FULL)
            src = write_source(td, "app")
            plan = plan_env(make_ctx(
                conf, binaries={"app": src}, secrets=("A=1\n", "x"),
                present_units=set(), exe_dirs=[]))
            paths = [f.path for f in plan.files]
            self.assertIn(layout.CADDY_FILE, paths)
            self.assertIn(layout.LITESTREAM_FILE, paths)
            self.assertIn("/etc/anci/units/app.service", paths)
            self.assertEqual([v.dest for v in plan.downloads],
                             [layout.LITESTREAM_BIN, layout.SQLITE_BIN, layout.CADDY_BIN])
            self.assertEqual(len(plan.blobs), 1)

    def test_activations_are_sorted_by_phase(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, FULL)
            src = write_source(td, "app")
            plan = plan_env(make_ctx(conf, binaries={"app": src},
                                     present_units=set(), exe_dirs=[]))
            phases = [a.phase for a in plan.after_apply]
            self.assertEqual(phases, sorted(phases))

    def test_planning_is_pure_no_connection(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            ctx = make_ctx(conf, binaries={"app": src},
                           present_units=set(), exe_dirs=[])
            self.assertIsNone(ctx.conn)
            plan_env(ctx)   # must not touch ssh
