"""The single-instance resources: present only when the conf wants them."""

import tempfile
import unittest
from pathlib import Path

from anci.inspect import Action, ObservedState, TargetState, diff
from anci.inspect.singletons import Caddy, Litestream, Logtrim, Secrets, Sshd

from .support import CONF, load_conf

REPLICATED = CONF + "\n[db.main-db.replication]\nbucket = 'backups'\nendpoint = 'https://s3.example.com'\naccess-key-env = 'S3_KEY'\nsecret-key-env = 'S3_SECRET'\n"


def _matching(resource, conf) -> dict[str, list[str]]:
    """Probe output for a box that has exactly what the conf wants."""
    want = resource.target(conf, "dev").get(resource.name, {})

    def lines(v):
        if isinstance(v, list):
            return v
        if isinstance(v, bool):
            return ["yes"] if v else []
        return [] if v is None else [v]
    return {k: lines(want.get(k)) if want else [] for k in resource.probes()}


class TestSingletons(unittest.TestCase):
    def test_always_present_ones_match_a_matching_box(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            for r in (Sshd(), Logtrim()):
                self.assertEqual(diff(ObservedState(r.observed(_matching(r, conf))),
                                      TargetState(r.target(conf, "dev"))), [], r.name)

    def test_litestream_only_with_replication(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(Litestream().target(load_conf(td), "dev"), {})
            self.assertIn("litestream", Litestream().target(load_conf(td, REPLICATED), "dev"))

    def test_litestream_left_after_replication_is_turned_off(self):
        with tempfile.TemporaryDirectory() as td:
            r = Litestream()
            found = _matching(r, load_conf(td, REPLICATED))
            [change] = diff(ObservedState(r.observed(found)), TargetState(r.target(load_conf(td), "dev")))
        self.assertEqual((change.identifier, change.action), ("litestream", Action.DELETE))

    def test_no_host_means_no_caddy(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(Caddy().target(load_conf(td), "dev"), {})

    def test_secrets_file_left_after_local_secrets_are_removed(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            self.assertEqual(Secrets().target(conf, "dev"), {})
            [change] = diff(ObservedState(Secrets().observed({"file": ["a" * 64]})),
                            TargetState(Secrets().target(conf, "dev")))
        self.assertEqual((change.identifier, change.action), ("secrets", Action.DELETE))

    def test_plaintext_secrets_are_hashed(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            (Path(td) / ".anci").mkdir()
            (Path(td) / ".anci" / "secrets.dev.env").write_text("K=v\n")
            self.assertIn("secrets", Secrets().target(conf, "dev"))


if __name__ == "__main__":
    unittest.main()
