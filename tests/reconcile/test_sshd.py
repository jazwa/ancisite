"""`sshd` — key-only logins on every box, checked against what sshd enforces."""

import tempfile
import unittest
from unittest import mock

from anci import errors
from anci.apply import ApplyResult
from anci.converge import sshd
from anci.ssh import Result

from .support import file_at, load_conf, make_ctx

KEY_ONLY = ("permitrootlogin without-password\npasswordauthentication no\n"
            "kbdinteractiveauthentication no\n")
CHANGED = ApplyResult(changed_files=[sshd.DROPIN])


def _run(stdout: str, result=ApplyResult(), pending=False):
    with mock.patch.object(sshd.ssh, "sudo", return_value=Result(stdout=stdout, stderr="")) as sudo:
        detail = sshd._activate(None, result, pending)
    return detail, sudo.call_args.args[1]


class TestSshd(unittest.TestCase):
    def test_every_env_gets_the_dropin_read_before_cloud_inits(self):
        with tempfile.TemporaryDirectory() as td:
            c = sshd.contribute(make_ctx(load_conf(td)))
        content = file_at(c, sshd.DROPIN).content
        self.assertIn("PasswordAuthentication no\n", content)
        self.assertIn("ChallengeResponseAuthentication no\n", content)
        self.assertLess(sshd.DROPIN.rsplit("/", 1)[1], "50-cloud-init.conf")

    def test_unchanged_only_checks(self):
        detail, script = _run(KEY_ONLY)
        self.assertNotIn("reload", script)
        self.assertIn("sshd -T", script)

    def test_changed_validates_then_reloads(self):
        for result, pending in ((CHANGED, False), (ApplyResult(), True)):
            detail, script = _run(KEY_ONLY, result, pending)
            self.assertLess(script.index("sshd -t"), script.index("systemctl reload"))
            self.assertIn(f"rm -f {sshd.DROPIN}", script)

    def test_rejected_config_is_an_error(self):
        with self.assertRaises(errors.EnvError) as cm:
            _run("invalid /etc/ssh/sshd_config line 3: Bad configuration option\n", CHANGED)
        self.assertIn("removed it", str(cm.exception))

    def test_passwords_still_on_is_an_error(self):
        for stdout in (KEY_ONLY.replace("passwordauthentication no", "passwordauthentication yes"),
                       KEY_ONLY.replace("kbdinteractiveauthentication no",
                                        "kbdinteractiveauthentication yes"),
                       KEY_ONLY.replace("without-password", "yes")):
            with self.assertRaises(errors.EnvError) as cm:
                _run(stdout)
            self.assertIn("still accepts password logins", str(cm.exception))

    def test_box_without_openssh_is_skipped(self):
        detail, script = _run("no-sshd\n", CHANGED)
        self.assertIsNone(detail)
        self.assertLess(script.index("command -v sshd"), script.index("sshd -t"))


if __name__ == "__main__":
    unittest.main()
