import unittest

from anci import versions as vmod
from anci.conf import ConfError, load
from anci.versions import VersionError

VALID = """
[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'
"""


def write_conf(tmp, text):
    p = tmp / "anci.toml"
    p.write_text(text)
    return p


class TestVersions(unittest.TestCase):
    def test_bundled_versions_load(self):
        entries = vmod.load()
        self.assertIn("0.0.1", entries)
        e = entries["0.0.1"]
        self.assertTrue(e.debian_image_url.startswith("https://cloud.debian.org/"))
        self.assertTrue(e.sha512)  # pinned image has a digest

    def test_unknown_version_error_lists_known(self):
        entries = vmod.load()
        with self.assertRaises(VersionError) as cm:
            vmod.get(entries, "9.9.9")
        self.assertIn("0.0.1", str(cm.exception))


class TestSopsBuilds(unittest.TestCase):
    def _load(self, text):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "versions.toml"
            p.write_text('["1.0"]\ndebian-image-url = "https://x/img"\n' + text)
            return vmod.load(p)["1.0"]

    def test_builds_parse(self):
        e = self._load('[ "1.0".sops-builds.macos-aarch64 ]\nurl = "https://x/s"\nsha256 = "ab"\n')
        self.assertEqual(e.sops_builds, {"macos-aarch64": ("https://x/s", "ab")})

    def test_missing_builds_is_empty(self):
        self.assertEqual(self._load("").sops_builds, {})

    def test_build_without_sha256_is_rejected(self):
        with self.assertRaises(VersionError) as cm:
            self._load('[ "1.0".sops-builds.linux-x86_64 ]\nurl = "https://x/s"\n')
        self.assertIn("sha256", str(cm.exception))


class TestLocalPlatform(unittest.TestCase):
    def test_names_match_the_release_binaries(self):
        from unittest import mock
        from anci import bin as bin_mod
        for system, machine, want in [("Darwin", "arm64", "macos-aarch64"),
                                      ("Darwin", "x86_64", "macos-x86_64"),
                                      ("Linux", "x86_64", "linux-x86_64"),
                                      ("Linux", "aarch64", "linux-aarch64"),
                                      ("Linux", "AMD64", "linux-x86_64")]:
            with mock.patch.object(bin_mod.platform, "system", return_value=system), \
                 mock.patch.object(bin_mod.platform, "machine", return_value=machine):
                self.assertEqual(bin_mod.local_platform(), want)


class TestSopsBinary(unittest.TestCase):
    def test_picks_this_platforms_build_and_caches_per_platform(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from anci import secrets
        conf = mock.Mock()
        conf.envs = {"dev": mock.Mock(anci_version="0.0.1")}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(secrets.paths, "bin_cache_dir", return_value=Path(td)), \
             mock.patch.object(secrets.bin_mod, "local_platform", return_value="macos-aarch64"), \
             mock.patch.object(secrets.bin_mod, "vendor", return_value=Path("/x")) as vendor:
            secrets.sops_binary(conf, "dev")
        url, _, sha256, name, _ = vendor.call_args.args
        self.assertTrue(url.endswith(".darwin.arm64"))
        self.assertEqual(sha256, "b97c0d434aab577dc40310e8d22ff9e45eef4c80638ab978daae9b4681c59286")
        self.assertEqual(name, "sops-3.13.3-macos-aarch64")

    def test_unsupported_platform_errors(self):
        from unittest import mock
        from anci import errors, secrets
        conf = mock.Mock()
        conf.envs = {"dev": mock.Mock(anci_version="0.0.1")}
        with mock.patch.object(secrets.bin_mod, "local_platform", return_value="freebsd-x86_64"), \
             self.assertRaises(errors.EnvError) as cm:
            secrets.sops_binary(conf, "dev")
        self.assertIn("freebsd-x86_64", str(cm.exception))


class TestVersionPinValidation(unittest.TestCase):
    """The conf requires a vm anci-version that exists in the bundled
    mapping — enforced at load time, so a typo fails fast."""

    def test_conf_pin_is_a_string(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            p = write_conf(Path(td), VALID)
            conf = load(p)
            self.assertEqual(conf.envs["dev"].anci_version, "0.0.1")

    def test_unknown_pin_is_rejected_at_load(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            bad = VALID.replace("'0.0.1'", "'9.9.9'")
            with self.assertRaises(ConfError) as cm:
                load(write_conf(Path(td), bad))
            self.assertIn("9.9.9", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
