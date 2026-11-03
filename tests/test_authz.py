"""Who you are (your ssh key) and what you may do (an env's access-users).

These tests never touch the network: where a verb would ssh, the
whole `anci.ssh` surface is mocked (partial mocks fall through to
real ssh and hang the suite).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import record_addresses
from anci import auth, errors
from anci.conf import Conf, User, load

CONF = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIdd5266e9506c16ebf7e12ffa7545d4c41feb0b2322d jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[env.staging]
ip = '198.51.100.10'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[env.prod]
ip = '203.0.113.10'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[exe.app]
source = './bin/server'

[exe.app.env.dev]

[exe.app.env.staging]

[exe.app.env.prod]
"""


def load_in(directory: str, text: str = CONF) -> Conf:
    p = Path(directory) / "anci.toml"
    p.write_text(text)
    return record_addresses(load(str(p)))


class TestAuthz(unittest.TestCase):
    def _conf(self, tmp):
        return load_in(tmp)

    def test_authorized_user_passes(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            auth, "identify_user", return_value="jack"
        ):
            self.assertEqual(auth.authorize(self._conf(td), "dev"), "jack")

    def test_key_not_in_conf_is_auth_error(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            auth, "identify_user", return_value="?"
        ):
            with self.assertRaises(errors.AuthError):
                auth.authorize(self._conf(td), "dev")

    def test_user_without_env_access_is_auth_error(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            auth, "identify_user", return_value="alice"
        ):
            with self.assertRaises(errors.AuthError):
                auth.authorize(self._conf(td), "dev")

    def test_missing_env_is_conf_error(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            auth, "identify_user", return_value="jack"
        ):
            with self.assertRaises(errors.ConfError):
                auth.authorize(self._conf(td), "nope")

class TestIdentifyUser(unittest.TestCase):
    def test_env_override_key_matches(self):
        # the CI/service-identity path: no agent, key at a nonstandard location
        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / "ci.pub"
            key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI040f3180240ae5aa323f1b40888c6ce9c965372500a ci@runner")
            conf = Conf(
                users={"ci": User(name="ci", ssh_pub_keys=["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI040f3180240ae5aa323f1b40888c6ce9c965372500a ci@runner"])},
                envs={},
                exes={},
            )
            with mock.patch.dict(os.environ, {"ANCI_SSH_KEY": str(key)}), mock.patch.object(
                auth.subprocess, "run", side_effect=OSError
            ):
                self.assertEqual(auth.identify_user(conf), "ci")


    def _identify(self, conf_key, agent_out):
        conf = Conf(users={"jack": User(name="jack", ssh_pub_keys=[conf_key])},
                    envs={}, exes={})
        with tempfile.TemporaryDirectory() as home, \
             mock.patch.dict(os.environ, {"ANCI_SSH_KEY": ""}), \
             mock.patch.object(auth.Path, "home", return_value=Path(home)), \
             mock.patch.object(auth.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout=agent_out)):
            return auth.identify_user(conf)

    def test_key_comment_does_not_matter(self):
        # conf says `jack`, the agent says `jack@dev-machine`: same key
        self.assertEqual(self._identify("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI2cd858682e6ccd72649c36334d147d3b0905e2e950b jack",
                                        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI2cd858682e6ccd72649c36334d147d3b0905e2e950b jack@dev-machine\n"), "jack")
        self.assertEqual(self._identify("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI2cd858682e6ccd72649c36334d147d3b0905e2e950b",
                                        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI2cd858682e6ccd72649c36334d147d3b0905e2e950b jack@dev-machine\n"), "jack")

    def test_falls_back_to_the_key_files(self):
        # no agent: the public keys in ~/.ssh are read instead
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIdd5266e9506c16ebf7e12ffa7545d4c41feb0b2322d jack"
        conf = Conf(users={"jack": User(name="jack", ssh_pub_keys=[key])}, envs={}, exes={})
        with tempfile.TemporaryDirectory() as home, \
             mock.patch.dict(os.environ, {"ANCI_SSH_KEY": ""}), \
             mock.patch.object(auth.Path, "home", return_value=Path(home)), \
             mock.patch.object(auth.subprocess, "run", side_effect=OSError):
            self.assertEqual(auth.identify_user(conf), "?")      # no agent, no key files
            (Path(home) / ".ssh").mkdir()
            (Path(home) / ".ssh" / "id_ed25519.pub").write_text(key)
            self.assertEqual(auth.identify_user(conf), "jack")

    def test_a_different_key_is_not_you(self):
        self.assertEqual(self._identify("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI2cd858682e6ccd72649c36334d147d3b0905e2e950b jack",
                                        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI39e2721ac7f63c57c9bd3129918e20e053145ec85d9 jack\n"), "?")


if __name__ == "__main__":
    unittest.main()
