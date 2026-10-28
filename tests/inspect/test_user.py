"""The user resource: what's read back, and that its target matches what the
accounts script writes."""

import os
import tempfile
import unittest
from unittest import mock

from anci.inspect import Action, ObservedState, TargetState, diff
from anci.inspect.core import sha
from anci.inspect import user as user_mod
from anci.inspect.user import User
from anci.render.shell import authorized_keys, sudoers_line
from anci.render import shell

from .support import load_conf
from .support import run_probes, write

KEY = "ssh-ed25519 AAAA jack"


class TestUser(unittest.TestCase):
    def _diff(self, found):
        with tempfile.TemporaryDirectory() as td:
            target = User().target(load_conf(td), "dev")
        return diff(ObservedState(User().observed(found)), TargetState(target))

    def test_matching_box_has_no_changes(self):
        found = {"group": ["jack"],
                 "sudoers": [f"jack {sha(sudoers_line('jack'))}"],
                 "keys": [f"jack {sha(authorized_keys([KEY]))}"]}
        self.assertEqual(self._diff(found), [])

    def test_a_revoked_user_keeps_their_sudoers_grant(self):
        found = {"group": ["jack"],
                 "sudoers": [f"jack {sha(sudoers_line('jack'))}", f"old {sha(sudoers_line('old'))}"],
                 "keys": [f"jack {sha(authorized_keys([KEY]))}"]}
        [change] = self._diff(found)
        self.assertEqual((change.identifier, change.action), ("user.old", Action.DELETE))
        self.assertEqual(change.fields, {"group": (False, None),
                                         "sudoers": (sha(sudoers_line("old")), None)})

    def test_new_user_is_a_create(self):
        [change] = self._diff({"group": [""], "sudoers": [], "keys": []})
        self.assertEqual((change.identifier, change.action), ("user.jack", Action.CREATE))

    def test_target_matches_the_grant(self):
        script = shell.grant_script("jack", [KEY])
        self.assertIn(f"printf '%s' '{sudoers_line('jack')}' > /etc/sudoers.d/anci-jack", script)
        self.assertIn(f"printf '%s' '{authorized_keys([KEY])}' > /home/jack/.ssh/authorized_keys", script)


class TestUserProbes(unittest.TestCase):
    def test_probes_read_group_grants_and_keys(self):
        with tempfile.TemporaryDirectory() as td:
            # a stub getent: jack is in the group; old was revoked but kept his grant
            write(f"{td}/bin/getent", "#!/bin/sh\necho 'anci:x:1001:jack'\n")
            os.chmod(f"{td}/bin/getent", 0o755)
            write(f"{td}/sudoers/anci-jack", sudoers_line("jack"))
            write(f"{td}/sudoers/anci-old", sudoers_line("old"))
            write(f"{td}/sudoers/other", "not anci's\n")
            write(f"{td}/home/jack/.ssh/authorized_keys", authorized_keys([KEY]))
            with mock.patch.object(user_mod, "_SUDOERS", f"{td}/sudoers"), \
                 mock.patch.object(user_mod, "_HOME", f"{td}/home"):
                found = run_probes(User(), env={**os.environ, "PATH": f"{td}/bin:{os.environ['PATH']}"})
        self.assertEqual(User().observed(found), {
            "user.jack": {"group": True, "sudoers": sha(sudoers_line("jack")),
                          "keys": sha(authorized_keys([KEY]))},
            "user.old": {"group": False, "sudoers": sha(sudoers_line("old")), "keys": None},
        })


if __name__ == "__main__":
    unittest.main()
