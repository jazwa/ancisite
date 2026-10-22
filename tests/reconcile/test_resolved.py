"""`resolved` — LLMNR off on every box, restarting resolved only on change."""

import tempfile
import unittest
from unittest import mock

from anci.apply import ApplyResult
from anci.converge import resolved

from .support import file_at, load_conf, make_ctx


class TestResolved(unittest.TestCase):
    def test_every_env_turns_llmnr_off(self):
        with tempfile.TemporaryDirectory() as td:
            c = resolved.contribute(make_ctx(load_conf(td)))
        self.assertIn("[Resolve]\nLLMNR=no\n", file_at(c, resolved.DROPIN).content)
        self.assertEqual(c.after_apply[0].watches, [resolved.DROPIN])

    def test_restarts_only_when_the_dropin_changed(self):
        run = resolved._activate
        with mock.patch.object(resolved.ssh, "sudo") as sudo:
            run(None, ApplyResult())
            sudo.assert_not_called()
            run(None, ApplyResult(changed_files=[resolved.DROPIN]))
            run(None, ApplyResult(), pending=True)
        self.assertEqual(sudo.call_count, 2)
        self.assertIn("is-active --quiet systemd-resolved.service", sudo.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
