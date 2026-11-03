import unittest

from anci import releases
from anci.conf import Conf, User


def make_conf():
    return Conf(
        users={"jack": User(name="jack", ssh_pub_keys=["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIdd5266e9506c16ebf7e12ffa7545d4c41feb0b2322d jack"])},
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
