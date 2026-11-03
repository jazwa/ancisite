"""`paths.box_id` — the per-project box discriminator."""

import tempfile
import unittest
from pathlib import Path

from anci import paths
from anci.conf import load

CONF = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIdd5266e9506c16ebf7e12ffa7545d4c41feb0b2322d jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }
"""


def _conf(directory: str):
    p = Path(directory) / "anci.toml"
    p.write_text(CONF)
    return load(str(p))


class TestBoxId(unittest.TestCase):
    def test_project_name_then_a_hash(self):
        with tempfile.TemporaryDirectory(prefix="myproj-") as td:
            bid = paths.box_id(_conf(td))
        self.assertRegex(bid, r"^myproj-[a-z0-9-]*-[0-9a-f]{6}$")

    def test_stable_for_a_project_and_different_across_projects(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self.assertEqual(paths.box_id(_conf(a)), paths.box_id(_conf(a)))
            self.assertNotEqual(paths.box_id(_conf(a)), paths.box_id(_conf(b)))


class TestPathAnchoring(unittest.TestCase):
    def test_artifacts_anchor_on_the_conf_dir_not_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(paths.artifacts_dir(_conf(td)), Path(td) / ".artifact")
