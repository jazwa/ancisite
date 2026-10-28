"""Coverage: authz, host-key pinning, secrets-diff.

These tests never touch the network: where a verb would ssh, the
whole `anci.ssh` surface is mocked (partial mocks fall through to
real ssh and hang the suite).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anci import box
from anci import auth, errors, paths, releases, ssh, ui
from anci.commands import dbs, status
from anci.conf import (
    Conf,
    Db,
    Env,
    Exe,
    ExeOverride,
    Replication,
    User,
    allocate_ports,
    load,
)

from .status_support import box_facts, observed

CONF = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[env.staging]
ip = '198.51.100.10'
access-users = ['jack']
mode = 'adopt'
vm = { anci-version = '0.0.1' }

[env.prod]
ip = '203.0.113.10'
access-users = ['jack']
mode = 'adopt'
vm = { anci-version = '0.0.1' }

[exe.app]
source = './bin/server'

[exe.app.env.dev]

[exe.app.env.staging]

[exe.app.env.prod]
"""


def load_in(directory: str, text: str = CONF) -> Conf:
    p = Path(directory) / "anci.toml"
    p.write_text(text)
    return load(str(p))


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
            key.write_text("ssh-ed25519 AAAACI ci@runner")
            conf = Conf(
                users={"ci": User(name="ci", ssh_pub_keys=["ssh-ed25519 AAAACI ci@runner"])},
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
        self.assertEqual(self._identify("ssh-ed25519 AAAAK jack",
                                        "ssh-ed25519 AAAAK jack@dev-machine\n"), "jack")
        self.assertEqual(self._identify("ssh-ed25519 AAAAK",
                                        "ssh-ed25519 AAAAK jack@dev-machine\n"), "jack")

    def test_a_different_key_is_not_you(self):
        self.assertEqual(self._identify("ssh-ed25519 AAAAK jack",
                                        "ssh-ed25519 BBBBK jack\n"), "?")


class TestPinning(unittest.TestCase):
    def test_local_env_gets_no_pinning(self):
        with tempfile.TemporaryDirectory() as td:
            conn = ssh.connect(load_in(td), "dev", "jack")
        self.assertIn("StrictHostKeyChecking=no", conn.opts)
        self.assertIn("UserKnownHostsFile=/dev/null", conn.opts)

    def test_pinned_remote_env_is_strict(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_in(td)
            pin = ssh.pin_path(conf, "staging")
            pin.parent.mkdir(parents=True, exist_ok=True)
            pin.write_text("198.51.100.10 ssh-ed25519 AAAA\n")
            conn = ssh.connect(conf, "staging", "jack")
        self.assertIn("StrictHostKeyChecking=yes", conn.opts)
        self.assertIn(f"UserKnownHostsFile={pin}", conn.opts)

    def test_unpinned_adopted_env_is_refused(self):
        # an adopted box is pinned by its first successful `up`: no pin = not set up
        with tempfile.TemporaryDirectory() as td:
            conf = load_in(td)
            with self.assertRaises(errors.EnvError) as cm:
                ssh.connect(conf, "staging", "jack")
            self.assertFalse(ssh.pin_path(conf, "staging").exists())
        self.assertIn("isn't set up", str(cm.exception))
        self.assertIn("up --user", cm.exception.try_)

    def test_unpinned_remote_host_guest_is_accept_new(self):
        import contextlib
        import io

        text = CONF + """
[env.lab]
ip = '198.51.100.20'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }
"""
        with tempfile.TemporaryDirectory() as td:
            conf = load_in(td, text)
            with mock.patch.object(ssh.box, "resolve_env_ip", return_value="192.168.122.9"), \
                 contextlib.redirect_stdout(io.StringIO()):  # silence the notice
                conn = ssh.connect(conf, "lab", "jack")
            # regression: ssh can't create the pin's parent dir, so connect must
            self.assertTrue(ssh.pin_path(conf, "lab").parent.is_dir())
        self.assertIn("StrictHostKeyChecking=accept-new", conn.opts)

    def test_never_prompts_for_a_password(self):
        with tempfile.TemporaryDirectory() as td:
            conn = ssh.connect(load_in(td), "dev", "jack")
        self.assertIn("BatchMode=yes", conn.opts)


class TestEnvTrust(unittest.TestCase):
    def _scan(self, stdout: str):
        return mock.patch.object(
            ssh.subprocess,
            "run",
            return_value=mock.Mock(returncode=0, stdout=stdout, stderr=""),
        )

    def test_trust_returns_all_scanned_key_types(self):
        import contextlib
        import io

        scanned = "198.51.100.10 ssh-ed25519 AAAA\n198.51.100.10 ssh-rsa BBBB\n"
        with tempfile.TemporaryDirectory() as td:
            conf = load_in(td)
            with self._scan(scanned), contextlib.redirect_stdout(io.StringIO()):
                keys = ssh.env_trust(conf, "staging", stdin=io.StringIO("y\n"))
            # the caller pins them once the box is set up
            self.assertFalse(ssh.pin_path(conf, "staging").exists())
        self.assertEqual(keys, ["198.51.100.10 ssh-ed25519 AAAA", "198.51.100.10 ssh-rsa BBBB"])

    def test_trust_abort_writes_nothing(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as td:
            conf = load_in(td)
            with self._scan("198.51.100.10 ssh-ed25519 AAAA\n"), contextlib.redirect_stdout(
                io.StringIO()
            ):
                keys = ssh.env_trust(conf, "staging", stdin=io.StringIO("n\n"))
            self.assertIsNone(keys)
            self.assertFalse(ssh.pin_path(conf, "staging").exists())


class TestPathAnchoring(unittest.TestCase):
    def test_artifacts_anchor_on_the_conf_dir_not_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_in(td)
            self.assertEqual(paths.artifacts_dir(conf), Path(td) / ".artifact")


class TestAppUrlGuards(unittest.TestCase):
    def test_unknown_env_is_conf_error(self):
        from anci.commands import deploy as dep

        with tempfile.TemporaryDirectory() as td:
            conf = load_in(td)
            with self.assertRaises(errors.ConfError):
                box.app_url(conf, "app", "nope")

    def test_unknown_exe_is_conf_error(self):
        from anci.commands import deploy as dep

        with tempfile.TemporaryDirectory() as td:
            conf = load_in(td)
            with self.assertRaises(errors.ConfError):
                box.app_url(conf, "nope", "dev")


class TestMigrateEnvGuard(unittest.TestCase):
    def test_unknown_env_is_conf_error(self):
        conf = Conf(
            users={"jack": User(name="jack", ssh_pub_keys=["k"])},
            envs={"dev": Env(name="dev", ip="127.0.0.1", access_users=["jack"],
                             anci_version="0.0.1")},
            exes={"app": Exe(name="app", source="./s",
                             env_overrides={"dev": ExeOverride(env="dev")})},
            dbs={"main-db": Db(name="main-db", migrations="./migrations/")},
        )
        with self.assertRaises(errors.ConfError):
            dbs.migrate(conf, "main-db", "nope")


class TestMechUnits(unittest.TestCase):
    def _conf(self) -> Conf:
        conf = Conf(
            users={"jack": User(name="jack", ssh_pub_keys=["k"])},
            # a remote env: there caddy is wanted only for a `host`
            envs={"dev": Env(name="dev", ip="10.0.0.5", access_users=["jack"],
                             anci_version="0.0.1")},
            exes={"app": Exe(name="app", source="./s",
                             env_overrides={"dev": ExeOverride(env="dev", host="dev.example.com")})},
            dbs={"main-db": Db(
                name="main-db", migrations="./m/",
                replication=Replication(bucket="backups"),
            )},
        )
        return conf

    def test_desired_mech_mirror_reconcile_conditions(self):
        conf = self._conf()
        self.assertEqual(
            status._mech_units(conf, "dev"),
            ["caddy.service", "litestream.service"],
        )
        # no replication, no host → both drop out
        conf.dbs["main-db"].replication = None
        conf.exes["app"].env_overrides["dev"].host = None
        self.assertEqual(status._mech_units(conf, "dev"), [])

    def test_reads_mech_facts(self):
        found = {
            "litestream.service active": ["active"],
            "litestream.service enabled": ["enabled"],
            "litestream.service installed": ["yes"],
            "caddy.service active": ["inactive"],
        }
        parsed = status._read(observed(found), self._conf(), ["app"], "dev")
        lit = parsed.mech["litestream.service"]
        self.assertEqual((lit.active, lit.enabled, lit.installed), ("active", True, True))
        cad = parsed.mech["caddy.service"]
        self.assertEqual(cad.active, "inactive")
        self.assertFalse(cad.installed)

    def test_mech_check_states(self):
        unit = "caddy.service"
        running = status._mech_check(unit, status.MechState(active="active", enabled=True, installed=True))
        self.assertIn("✓", running)
        self.assertIn(f"{unit} — running", running)
        self.assertNotIn("not enabled", running)
        stopped = status._mech_check(unit, status.MechState(active="inactive", enabled=False, installed=True))
        self.assertIn("✗", stopped)
        self.assertIn(f"{unit} — stopped", stopped)
        self.assertIn("· not enabled at boot", stopped)
        missing = status._mech_check("litestream.service", status.MechState(active="inactive", enabled=False, installed=False))
        self.assertIn(f"litestream.service — not installed", missing)
        self.assertNotIn("not enabled", missing)   # no unit file → boot is moot
        gap = status._mech_check(unit, None)
        self.assertIn("—", gap)
        self.assertIn(unit, gap)

    def _status_output(self, conf, found: dict, debug: bool) -> str:
        import contextlib
        import io

        buf = io.StringIO()
        with mock.patch.object(auth, "identify_user", return_value="jack"), \
             mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
             mock.patch.object(ssh, "facts", return_value=box_facts(found)), \
             contextlib.redirect_stdout(buf):
            status.status(conf, "dev", debug=debug)
        return buf.getvalue()

    def test_status_prints_mechanism_checklist(self):
        conf = self._conf()
        found = {
            "app active": ["active"], "app port": ["open"],
            "litestream.service active": ["active"],
            "litestream.service enabled": ["enabled"],
            "caddy.service active": ["active"],
            "caddy.service enabled": ["enabled"],
            "caddy.service installed": ["yes"],
        }
        text = self._status_output(conf, found, debug=False)
        # litestream left the checklist — the db table's replication
        # column speaks for it per-db (here: db never deployed → all dashes)
        self.assertNotIn("litestream.service", text)
        self.assertIn("main-db", text)
        self.assertIn("caddy.service — running", text)
        # the env HAS a host → caddy is real, no "not configured" note
        self.assertNotIn("not configured", text)
        # the ssh hint moved out from above the table into the checklist
        self.assertIn("ssh with ssh -J jack@10.0.0.5 jack@", text)   # a remote hypervisor's box
        self.assertNotIn("boot", text)   # no table header/columns
        self.assertNotIn("systemd", text)  # no section header either

    def test_unset_caddy_note_when_no_host(self):
        conf = self._conf()
        conf.exes["app"].env_overrides["dev"].host = None   # caddy drops out of the desired list
        found = {
            "app active": ["active"], "app port": ["open"],
            "litestream.service active": ["active"],
            "litestream.service enabled": ["enabled"],
            "litestream.service installed": ["yes"],
        }
        text = self._status_output(conf, found, debug=False)
        line = next(
            ln for ln in text.splitlines() if "not configured" in ln
        )
        self.assertIn("✗", line)
        self.assertIn("caddy.service — not configured (no host)", line)
        self.assertNotIn("litestream.service", text)  # per-db, in the table

    def test_unset_caddy_note_skipped_for_pure_jobs(self):
        # nothing long-running → nothing to front → no note, no caddy
        conf = self._conf()
        conf.exes["app"].env_overrides["dev"].host = None
        conf.exes["app"].schedule = "daily"
        self.assertNotIn("not configured", self._status_output(conf, {}, debug=False))
    def test_status_down_grays_the_checklist(self):
        import contextlib
        import io

        conf = self._conf()
        buf = io.StringIO()
        with mock.patch.object(auth, "identify_user", return_value="jack"), \
             mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
             mock.patch.object(ssh, "facts",
                               side_effect=ssh.SSHError("no route", unreachable=True)), \
             contextlib.redirect_stdout(buf):
            status.status(conf, "dev", debug=False)
        text = buf.getvalue()
        self.assertIn("box down", text)
        self.assertIn("— caddy.service", text)
        self.assertNotIn("litestream.service", text)  # per-db, in the table
        self.assertIn("main-db", text)                # db row grayed too
        self.assertNotIn("ssh with", text)            # no login hint for a down box

class TestStatusReleases(unittest.TestCase):
    def test_last_deploy_is_attributed_per_exe(self):
        found = {"releases": [
            'app\t{"ts":"2026-01-01T00:00:00Z","env":"dev","blob":"aaaa","source":"s","user":"jack"}',
            'worker\t{"ts":"2026-02-02T00:00:00Z","env":"dev","blob":"bbbb","source":"s","user":"alice"}',
        ]}
        conf = Conf(
            users={}, envs={"dev": Env(name="dev", ip="127.0.0.1", access_users=[],
                                       anci_version="0.0.1")},
            exes={n: Exe(name=n, source="./s") for n in ("app", "worker")},
        )
        parsed = status._read(observed(found), conf, ["app", "worker"], "dev")
        self.assertEqual(status._latest_release(parsed.releases, "app", "dev")["user"], "jack")
        self.assertEqual(
            status._latest_release(parsed.releases, "worker", "dev")["user"], "alice"
        )


class TestUITableAnsi(unittest.TestCase):
    def test_columns_align_with_color_codes(self):
        ui.set_ansi(True)
        try:
            table = ui.table(
                ["exe", "state"], [["app", ui.green("serving")], ["web", "down"]]
            )
        finally:
            ui.set_ansi(False)
        widths = {len(ui._ANSI_RE.sub("", line)) for line in table.splitlines()}
        self.assertEqual(len(widths), 1, f"ragged table:\n{table}")


class TestAllocatePortsBoundary(unittest.TestCase):
    def _conf(self, n: int) -> Conf:
        return Conf(
            users={},
            envs={"dev": Env(name="dev", ip="127.0.0.1", access_users=[],
                             anci_version="0.0.1")},
            exes={
                f"x{i}": Exe(
                    name=f"x{i}", source="./s",
                    env_overrides={"dev": ExeOverride(env="dev")},
                )
                for i in range(n)
            },
        )

    def test_ten_exes_per_env_fit(self):
        ports = allocate_ports(self._conf(10))
        self.assertEqual(len(set(ports.values())), 10)

    def test_eleven_exes_are_rejected(self):
        with self.assertRaises(errors.ConfError):
            allocate_ports(self._conf(11))


if __name__ == "__main__":
    unittest.main()
