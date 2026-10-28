import unittest
from unittest import mock

from anci import auth, releases
from anci.conf import Conf, User


def make_conf():
    return Conf(
        users={"jack": User(name="jack", ssh_pub_keys=["ssh-ed25519 AAAA jack"])},
        envs={}, exes={},
    )


class TestLatestRelease(unittest.TestCase):
    """status reads the release log; a torn trailing line must not break it."""

    def test_latest_per_exe_and_env(self):
        import json
        from anci.commands import status

        def rec(ts, env, user):
            return json.dumps({"ts": ts, "env": env, "blob": "b", "source": "s", "user": user})

        lines = [("demo", rec("2026-09-30T14:00:00Z", "dev", "jack")),
                 ("demo", rec("2026-09-30T15:00:00Z", "dev", "alice")),
                 ("demo", rec("2026-09-30T16:00:00Z", "prod", "bob")),
                 ("demo", '{"ts": "2026-09-30T17')]          # torn trailing line
        self.assertEqual(status._latest_release(lines, "demo", "dev")["user"], "alice")
        self.assertIsNone(status._latest_release(lines, "other", "dev"))


class TestIdentifyUser(unittest.TestCase):
    def test_matches_by_key(self):
        with mock.patch.object(auth.subprocess, "run", side_effect=OSError):
            with mock.patch.object(auth.Path, "home") as fake_home:
                fake = fake_home.return_value / "x"  # unused; patch read path below
                # provide key via ~/.ssh/id_ed25519.pub
                import tempfile
                from pathlib import Path

                with tempfile.TemporaryDirectory() as td:
                    ssh_dir = Path(td) / ".ssh"
                    ssh_dir.mkdir()
                    (ssh_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAA jack")
                    fake_home.return_value = Path(td)
                    self.assertEqual(auth.identify_user(make_conf()), "jack")

    def test_unknown_when_no_match(self):
        with mock.patch.object(auth.subprocess, "run", side_effect=OSError):
            with mock.patch.object(auth.Path, "home") as fake_home:
                import tempfile
                from pathlib import Path

                with tempfile.TemporaryDirectory() as td:
                    fake_home.return_value = Path(td)
                    self.assertEqual(auth.identify_user(make_conf()), "?")


class TestAppend(unittest.TestCase):
    def test_appends_jsonl_line(self):
        # run as root (apply's script), so any deployer can append regardless of umask
        s = releases.append_script("demo", env="dev", blob="abc", source="./s", user="jack")
        self.assertIn(f"install -d -o root -g anci -m 2775 {releases.RELEASES_DIR}/demo", s)
        self.assertIn(">>", s)
        self.assertIn('{"ts":', s.replace("'", ""))
        self.assertIn('"user":"jack"', s)
        self.assertNotIn('"static"', s)

    def test_records_the_static_version_when_shipped(self):
        s = releases.append_script("demo", env="dev", blob="abc", source="./s", user="jack",
                                   static="0123456789abcdef")
        self.assertIn('"static":"0123456789abcdef"', s)


if __name__ == "__main__":
    unittest.main()
