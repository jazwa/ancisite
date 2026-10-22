"""App logs stay bounded (the hourly trim), and the release log keeps its newest lines."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anci import releases, ssh, units
from anci.converge import logtrim, orphans
from anci.render import template

from .support import load_conf, make_ctx


class TestLogTrim(unittest.TestCase):
    def test_every_box_gets_the_timer_and_keeps_it(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            c = logtrim.contribute(make_ctx(conf))
            self.assertIn(units.LOGTRIM_TIMER, {Path(f.path).name for f in c.files})
            # cleanup must not treat it as an orphan, and regen must not stop it
            self.assertLessEqual({units.LOGTRIM_SERVICE, units.LOGTRIM_TIMER},
                                 orphans.desired_unit_names(conf, "dev"))
            self.assertLessEqual({units.LOGTRIM_SERVICE, units.LOGTRIM_TIMER}, units.MECHANISM_UNITS)

    def test_script_trims_only_logs_past_the_limit(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "big.log").write_text("x" * 20)
            (Path(td) / "small.log").write_text("y" * 5)
            script = template("logtrim.sh", LOG_DIR=td, LIMIT="10")
            subprocess.run(["sh", "-c", script], check=True)
            self.assertEqual((Path(td) / "big.log").read_text(), "")
            self.assertEqual((Path(td) / "big.log.1").read_text(), "x" * 20)
            self.assertEqual((Path(td) / "small.log").read_text(), "y" * 5)
            self.assertFalse((Path(td) / "small.log.1").exists())


class TestReleaseLog(unittest.TestCase):
    def test_append_trims_in_batches(self):
        with mock.patch.object(ssh, "sudo") as sudo:
            releases.append(None, "app", env="dev", blob="b", source="s", user="u")
        script = sudo.call_args.args[1]
        self.assertIn(f"-ge {releases.RELEASES_MAX} ]", script)      # trims on reaching 16k…
        self.assertIn(f"tail -n {releases.RELEASES_KEPT}", script)   # …down to the newest 8k
        self.assertEqual((releases.RELEASES_MAX, releases.RELEASES_KEPT), (16384, 8192))


if __name__ == "__main__":
    unittest.main()
