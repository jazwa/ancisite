import unittest

from anci.render import litestream, shell
from anci import layout


class TestLayout(unittest.TestCase):
    def test_shared_dirs_root_anci_2775(self):
        script = shell.render_layout_script()
        self.assertIn(f"-o root -g {layout.ANCI_GROUP} -m 2775", script)
        self.assertIn(layout.VAR_DIR, script)          # /var/lib/anci itself (finding #4)
        self.assertIn(layout.BLOB_DIR, script)

    def test_config_dirs_root_root_0755(self):
        script = shell.render_layout_script()
        self.assertIn("-o root -g root -m 0755", script)
        for d in (layout.ETC_DIR, layout.UNITS_DIR, layout.CADDY_DIR):
            self.assertIn(d, script)

    def test_log_dir_is_app_owned(self):
        script = shell.render_layout_script()
        self.assertIn(
            f"-o {layout.ANCI_USER} -g {layout.ANCI_GROUP} -m {layout.LOG_MODE} {layout.LOG_DIR}",
            script,
        )

    def test_no_env_in_guest_paths(self):
        # one env = one box, so no guest path carries an env segment
        self.assertEqual(layout.log_path("app"), "/var/log/anci/app.log")
        self.assertEqual(layout.LITESTREAM_FILE, "/etc/anci/litestream.yaml")
        self.assertEqual(layout.SECRETS_FILE, "/etc/anci/secrets.env")

    def test_shared_and_config_are_disjoint(self):
        self.assertFalse(set(layout.SHARED_DIRS) & set(layout.CONFIG_DIRS))


if __name__ == "__main__":
    unittest.main()


class TestSafeNames(unittest.TestCase):
    def test_names_anci_creates_are_safe(self):
        for ok in ("app", "my-app", "app.v2", "job_1", "0day", "caddy.service", "x.timer"):
            self.assertTrue(layout.is_safe_name(ok), ok)

    def test_everything_else_is_not(self):
        for bad in ("", ".", "..", ".hidden", "-rf", "a b", "a;b", "$(id)", "`id`", "a/b",
                    "a\nb", "a\tb", "ünï"):
            self.assertFalse(layout.is_safe_name(bad), repr(bad))

    def test_split_keeps_order(self):
        self.assertEqual(layout.split_safe(["b", "x y", "a", "c;d"]), (["b", "a"], ["x y", "c;d"]))


class TestBlobNaming(unittest.TestCase):
    def test_id_is_the_first_16_hex_of_the_sha(self):
        sha = "0123456789abcdef" + "f" * 48
        self.assertEqual(layout.blob_id(sha), "0123456789abcdef")
        self.assertEqual(layout.blob_path(sha), f"{layout.BLOB_DIR}/0123456789abcdef")
