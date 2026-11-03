import tempfile
import unittest
from pathlib import Path

from anci.commands.init import build_init_conf, detect_source, normalize_name


class TestBuildInitConf(unittest.TestCase):
    def test_contains_all_sections(self):
        conf = build_init_conf(
            name="blog", source="./target/release/blog",
            ssh_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfe6f8f66008145931ca3483cf80e71d3d6431ed09e1 me", user="jack",
        )
        self.assertIn("[user.jack]", conf)
        self.assertIn("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfe6f8f66008145931ca3483cf80e71d3d6431ed09e1 me", conf)
        self.assertIn("[env.dev]", conf)
        self.assertIn("ip = '127.0.0.1'", conf)
        self.assertIn("access-users = ['jack']", conf)
        self.assertIn("anci-version = '0.0.1'", conf)
        self.assertIn("vm = { vcpus = 2, mem = '1gb', disk = '20gb' }", conf)
        self.assertIn("[exe.blog]", conf)
        self.assertIn("source = './target/release/blog'", conf)
        # the generated conf must be parseable by our own strict parser
        import tempfile

        from anci.conf import load

        fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        fd.write(conf)
        fd.close()
        parsed = load(fd.name)
        self.assertEqual(parsed.exes["blog"].source, "./target/release/blog")

    def test_user_defaults_to_you(self):
        conf = build_init_conf(name="blog", source="./s", ssh_key=None)
        self.assertIn("[user.you]", conf)
        self.assertIn("access-users = ['you']", conf)

    def test_placeholder_key_uses_the_username(self):
        conf = build_init_conf(name="blog", source="./s", ssh_key=None, user="jack")
        self.assertIn("jack@dev-machine", conf)

    def test_placeholder_key_when_none_found(self):
        conf = build_init_conf(name="blog", source="./s", ssh_key=None)
        self.assertIn("PASTE-YOUR-PUBLIC-KEY-HERE", conf)

    def test_fallback_source_comment_is_not_inside_the_quotes(self):
        # regression: the "TODO: build first" note used to be embedded in the
        # quoted value, making the generated source path unusable
        conf = build_init_conf(name="blog", source=None, ssh_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfe6f8f66008145931ca3483cf80e71d3d6431ed09e1 me")
        import re
        import tempfile

        from anci.conf import load

        m = re.search(r"^source = '([^']*)'", conf, re.M)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "./target/release/blog")

        fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        fd.write(conf)
        fd.close()
        parsed = load(fd.name)
        self.assertEqual(parsed.exes["blog"].source, "./target/release/blog")


class TestDetectSource(unittest.TestCase):
    def test_detects_existing_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            (cwd / "target" / "release").mkdir(parents=True)
            (cwd / "target" / "release" / "blog").write_text("x")
            self.assertEqual(detect_source(cwd, "blog"), "./target/release/blog")

    def test_none_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(detect_source(Path(td), "nope"))


class TestNormalizeName(unittest.TestCase):
    def test_normalizes_to_a_name_the_conf_accepts(self):
        self.assertEqual(normalize_name("Jack Smith", "you"), "jack-smith")
        self.assertEqual(normalize_name("my_app", "you"), "my-app")
        self.assertEqual(normalize_name("  Blog  ", "you"), "blog")
        self.assertEqual(normalize_name("x" * 40, "you"), "x" * 32)

    def test_empty_or_unusable_falls_back(self):
        self.assertEqual(normalize_name("   ", "you"), "you")
        self.assertEqual(normalize_name("2048", "you"), "you")


class TestInitCommand(unittest.TestCase):
    def test_init_runs_without_an_existing_conf(self):
        # regression: main() called find_conf() *before* the `init` special-case,
        # so `anci init` failed in a fresh directory — the only place it's used
        from unittest import mock

        from anci import cli
        from anci.commands import init as init_mod

        with mock.patch.object(cli, "find_conf",
                               side_effect=AssertionError("find_conf was called")), \
             mock.patch.object(init_mod, "init", return_value=0) as init_mock:
            rc = cli.main(["init"])
        self.assertEqual(rc, 0)
        init_mock.assert_called_once()

    def test_prompts_for_and_writes_the_username(self):
        # regression: init must thread the prompted username into the conf
        from unittest import mock

        from anci.commands import init as init_mod

        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            stdin = mock.Mock()
            stdin.isatty.return_value = True
            with mock.patch.object(init_mod.sys, "stdin", stdin), \
                 mock.patch("builtins.input",
                            side_effect=["jack", "blog", ""]) as ask, \
                 mock.patch.object(init_mod, "detect_ssh_key",
                                   return_value="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfe6f8f66008145931ca3483cf80e71d3d6431ed09e1 me"):
                rc = init_mod.init(cwd)
            self.assertEqual(rc, 0)
            conf = (cwd / "anci.toml").read_text()
        self.assertIn("your username [", ask.call_args_list[0].args[0])
        self.assertIn("[user.jack]", conf)
        self.assertIn("access-users = ['jack']", conf)
        self.assertIn("[exe.blog]", conf)


if __name__ == "__main__":
    unittest.main()
