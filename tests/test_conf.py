import unittest
from unittest import mock

from anci import validate
from anci.conf import ConfError, allocate_ports, load
from anci.conf import mem_to_kib, disk_to_qemu

VALID = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[user.alice]
ssh-pub-keys = ['ssh-ed25519 AAAA alice', 'ssh-ed25519 AAAA alice2']

[env.dev]
ip = '127.0.0.1'
access-users = ['jack', 'alice']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '2gb', disk = '20gb' }

[env.prod]
ip = '203.0.113.10'
access-users = ['jack']
mode = 'adopt'
vm = { anci-version = '0.0.1' }

[exe.app]
source = './bin/server'
args = ['-config', '/etc/app.toml']
env-vars = { LOG_LEVEL = 'info' }

[exe.app.env.dev]
env-vars = { LOG_LEVEL = 'debug', EXTRA = 'yes' }

[exe.app.env.prod]

[exe.worker]
source = './bin/worker'

[exe.worker.env.dev]
"""

from pathlib import Path
import tempfile


def load_str(toml_text: str):
    fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    fd.write(toml_text)
    fd.close()  # flush to disk BEFORE parsing (the old in-with-block load read a partial file)
    return load(fd.name)


class TestLoadValid(unittest.TestCase):
    def test_parses_entities(self):
        conf = load_str(VALID)
        self.assertEqual(set(conf.users), {"jack", "alice"})
        self.assertEqual(set(conf.envs), {"dev", "prod"})
        self.assertEqual(conf.envs["dev"].vcpus, 2)
        self.assertEqual(conf.envs["prod"].anci_version, "0.0.1")
        self.assertEqual(conf.exes["app"].args, ["-config", "/etc/app.toml"])

    def test_every_exe_runs_on_every_env(self):
        conf = load_str(VALID)
        # presence is uniform: worker has no prod subtable but still gets a
        # port there — a subtable never switches an exe off somewhere
        self.assertIn(("worker", "prod"), allocate_ports(conf))

    def test_resolve_merges_env_vars_deep(self):
        conf = load_str(VALID)
        r = conf.exes["app"].resolve_for("dev")
        self.assertEqual(r.env_vars, {"LOG_LEVEL": "debug", "EXTRA": "yes"})
        self.assertEqual(r.args, ["-config", "/etc/app.toml"])  # inherited
        self.assertEqual(r.target, "/var/lib/anci/exe/app/bin")  # derived from the name

    def test_resolve_scalar_replacement(self):
        conf = load_str(VALID)
        r = conf.exes["app"].resolve_for("prod")  # empty override
        self.assertEqual(r.env_vars, {"LOG_LEVEL": "info"})

    def test_target_is_derived_from_the_name(self):
        conf = load_str(VALID)
        self.assertEqual(conf.exes["app"].resolve_for("dev").target,
                         "/var/lib/anci/exe/app/bin")
        self.assertEqual(conf.exes["app"].resolve_for("prod").target,
                         "/var/lib/anci/exe/app/bin")

    def test_users_for_env(self):
        conf = load_str(VALID)
        users = conf.users_for_env("dev")
        self.assertEqual(set(users), {"jack", "alice"})
        self.assertEqual(len(users["alice"]), 2)  # alice has two keys
        self.assertEqual(len(users["jack"]), 1)

    def test_port_allocation_deterministic(self):
        conf = load_str(VALID)
        ports = allocate_ports(conf)
        # each env is its own box, so ports restart per env
        self.assertEqual(ports[("app", "dev")], 8000)
        self.assertEqual(ports[("worker", "dev")], 8010)
        self.assertEqual(ports[("app", "prod")], 8000)
        self.assertEqual(allocate_ports(conf), ports)


PORTED = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.prod]
ip = '203.0.113.10'
access-users = ['jack']
port = 2222
mode = 'adopt'
vm = { anci-version = '0.0.1' }

[exe.app]
source = './x'

[exe.app.env.prod]
"""


class TestEnvPort(unittest.TestCase):
    def test_default_is_22(self):
        self.assertEqual(load_str(VALID).envs["dev"].port, 22)

    def test_custom_port_parses(self):
        self.assertEqual(load_str(PORTED).envs["prod"].port, 2222)

    def test_out_of_range_or_nonint_rejected(self):
        for bad in ("port = 0", "port = 70000", "port = 'x'"):
            with self.assertRaises(ConfError):
                load_str(PORTED.replace("port = 2222", bad))


class TestSizes(unittest.TestCase):
    def test_mem(self):
        self.assertEqual(mem_to_kib("4gb"), 4 * 1024 * 1024)
        self.assertEqual(mem_to_kib("512mb"), 512 * 1024)
        # a bare integer is MiB everywhere (virt-install and the resize plan agree)
        self.assertEqual(mem_to_kib(2048), 2048 * 1024)

    def test_mem_to_mib_virt_install_units(self):
        # regression: virt-install --memory is MiB. Passing KiB here once made
        # anci request a TiB of guest RAM (instant "Cannot allocate memory").
        from anci.conf import mem_to_mib

        self.assertEqual(mem_to_mib("1gb"), 1024)
        self.assertEqual(mem_to_mib("2gb"), 2048)
        self.assertEqual(mem_to_mib("512mb"), 512)
        self.assertEqual(mem_to_mib(2048), 2048)

    def test_disk(self):
        self.assertEqual(disk_to_qemu("20gb"), "20G")
        self.assertEqual(disk_to_qemu("512mb"), "512M")
        self.assertEqual(disk_to_qemu(40), "40G")


class TestValidation(unittest.TestCase):
    def assert_conf_error(self, text, fragment):
        with self.assertRaises(ConfError) as cm:
            load_str(text)
        self.assertIn(fragment, str(cm.exception))

    def test_exe_names_with_the_same_port_var_are_refused(self):
        self.assert_conf_error(VALID + "\n[exe.my-api]\nsource = './a'\n[exe.my_api]\nsource = './b'\n",
                               "ANCI_PORT_MY_API")

    def test_replication_defaults_and_opt_in(self):
        conf = load_str(VALID + """
[replication]
bucket = 'my-bucket'
endpoint = 'https://acct.r2.cloudflarestorage.com'
region = 'us-east-1'
access-key-env = 'R2_ACCESS_KEY_ID'
secret-key-env = 'R2_SECRET_ACCESS_KEY'
retention = '720h'

[db.main-db]
migrations = './m'

[db.main-db.replication]
sync-interval = '5s'
""")
        r = conf.dbs["main-db"].replication
        self.assertEqual(r.bucket, "my-bucket")           # from the defaults
        self.assertEqual(r.endpoint, "https://acct.r2.cloudflarestorage.com")
        self.assertEqual(r.region, "us-east-1")           # optional signing region
        self.assertEqual(r.access_key_env, "R2_ACCESS_KEY_ID")
        self.assertEqual(r.retention, "720h")
        self.assertEqual(r.sync_interval, "5s")           # per-db override wins
        self.assertIsNone(r.path)                         # defaults to the db name later
        self.assertIsNone(r.path)                         # defaults to the db name later
        plain = load_str(VALID + "\n[db.plain]\nmigrations = './m'\n")
        self.assertIsNone(plain.dbs["plain"].replication)  # no subtable = off

    def test_bad_replication_is_refused_with_its_table_named(self):
        repl = "\n[replication]\nbucket = 'b'\n[db.d]\nmigrations = './m'\n[db.d.replication]\n"
        for extra, fragment in (
            ("bucket = 3\n", "'bucket' must be a non-empty string"),
            ("bucket = 'my bucket'\n", "isn't a valid bucket name"),
            ("endpoint = 'r2.example.com'\n", "must be a URL"),
            ("path = '/abs'\n", "relative key prefix"),
            ("path = 'a/../b'\n", "relative key prefix"),
            ("path = 'shared'\n", "replicate to the same place"),   # dev and prod collide
        ):
            self.assert_conf_error(VALID + repl + extra, fragment)
        self.assert_conf_error(VALID + "\n[db.d]\nmigrations = './m'\n[db.d.replication]\n",
                               "[db.d.replication]: 'bucket' is required")

    def test_replication_per_env(self):
        conf = load_str(VALID + """
[replication]
bucket = 'my-bucket'

[db.only-prod]
migrations = './m'
[db.only-prod.env.prod.replication]

[db.not-dev]
migrations = './m'
[db.not-dev.replication]
sync-interval = '5s'
[db.not-dev.env.dev]
replication = false
[db.not-dev.env.prod.replication]
path = 'prod/not-dev'
""")
        self.assertEqual([d.name for d in conf.replicated_dbs("prod")], ["only-prod", "not-dev"])
        self.assertEqual(conf.replicated_dbs("dev"), [])
        prod = conf.dbs["not-dev"].resolve_for("prod").replication
        self.assertEqual((prod.bucket, prod.sync_interval, prod.path),
                         ("my-bucket", "5s", "prod/not-dev"))   # overlays the base table
        self.assertIsNone(conf.dbs["only-prod"].replication)    # the base stays off
        # the replica lives under <env>/<db> unless `path` is set
        self.assertEqual(conf.dbs["only-prod"].resolve_for("prod").replication.path, "prod/only-prod")

    def test_db_env_tables_are_checked(self):
        db = "\n[replication]\nbucket = 'b'\n[db.d]\nmigrations = './m'\n"
        self.assert_conf_error(VALID + db + "[db.d.env.nope]\nreplication = false\n",
                               "env 'nope' is not defined")
        self.assert_conf_error(VALID + db + "[db.d.env.dev]\nreplication = true\n",
                               "must be a table, or false")
        self.assert_conf_error(VALID + db + "[db.d.env.dev]\nmigrations = './x'\n", "migrations")

    def test_replication_per_db_override(self):
        conf = load_str(VALID + """
[replication]
bucket = 'my-bucket'
endpoint = 'https://acct.r2.cloudflarestorage.com'
region = 'us-east-1'

[db.main-db]
migrations = './m'

[db.main-db.replication]
bucket = 'other-bucket'
region = 'eu-west-1'
""")
        self.assertEqual(conf.dbs["main-db"].replication.bucket, "other-bucket")
        self.assertEqual(conf.dbs["main-db"].replication.endpoint,
                         "https://acct.r2.cloudflarestorage.com")
        self.assertEqual(conf.dbs["main-db"].replication.region, "eu-west-1")

    def test_replication_string_form_rejected(self):
        self.assert_conf_error(
            VALID + "\n[db.main-db]\nmigrations = './m'\n"
                    "replication = 'litestream:s3://bucket/db'",
            "'replication' must be a table")

    def test_replication_bucket_required(self):
        self.assert_conf_error(
            VALID + "\n[db.main-db]\nmigrations = './m'\n"
                    "[db.main-db.replication]\nsync-interval = '1s'",
            "'bucket' is required")

    def test_replication_unpaired_env_names_rejected(self):
        self.assert_conf_error(
            VALID + "\n[db.main-db]\nmigrations = './m'\n"
                    "[db.main-db.replication]\nbucket = 'b'\n"
                    "access-key-env = 'R2_KEY'",
            "go together")

    def test_replication_bad_env_name_rejected(self):
        self.assert_conf_error(
            VALID + "\n[db.main-db]\nmigrations = './m'\n"
                    "[db.main-db.replication]\nbucket = 'b'\n"
                    "access-key-env = '1BAD'\nsecret-key-env = 'OK'",
            "isn't a valid env var name")

    def test_replication_bad_duration_rejected(self):
        self.assert_conf_error(
            VALID + "\n[db.main-db]\nmigrations = './m'\n"
                    "[db.main-db.replication]\nbucket = 'b'\n"
                    "retention = '30 days'",
            "must be a Go duration")

    def test_replication_bad_region_rejected(self):
        self.assert_conf_error(
            VALID + "\n[db.main-db]\nmigrations = './m'\n"
                    "[db.main-db.replication]\nbucket = 'b'\nregion = ''",
            "'region' must be a non-empty string")

    def test_replication_unknown_key_rejected(self):
        self.assert_conf_error(
            VALID + "\n[db.main-db]\nmigrations = './m'\n"
                    "[db.main-db.replication]\nbucket = 'b'\nurl = 's3://b/x'",
            "unknown key(s)")

    def test_unknown_top_key(self):
        self.assert_conf_error(VALID + "\n[typo]\nx = 1", "unknown key(s)")

    def test_unknown_env_key(self):
        self.assert_conf_error(
            VALID.replace("access-users = ['jack', 'alice']", "access_user = ['jack']"),
            "unknown key",
        )

    def test_old_vm_reference_rejected(self):
        # the pre-collapse form `vm = 'my-vm'` is no longer valid
        self.assert_conf_error(
            VALID.replace(
                "vm = { anci-version = '0.0.1', vcpus = 2, mem = '2gb', disk = '20gb' }",
                "vm = 'my-vm'",
            ),
            "'vm' must be a table",
        )

    def test_undefined_user_in_access(self):
        self.assert_conf_error(
            VALID.replace("access-users = ['jack', 'alice']", "access-users = ['ghost']"),
            "'ghost'",
        )

    def test_exe_with_no_subtables_is_accepted(self):
        # an exe with no [exe.X.env.Y] subtables isn't orphaned anymore:
        # every exe runs on every env; subtables are config overrides only
        conf = load_str(VALID + "\n[exe.lonely]\nsource = './x'")
        self.assertIn(("lonely", "dev"), allocate_ports(conf))
        self.assertIn(("lonely", "prod"), allocate_ports(conf))

    def test_exe_on_undefined_env(self):
        self.assert_conf_error(
            VALID + "\n[exe.app.env.ghost]",
            "not defined",
        )

    def test_target_key_rejected(self):
        # `target` is fixed now: the on-guest path is derived from the exe name
        self.assert_conf_error(
            VALID.replace("source = './bin/server'",
                          "source = './bin/server'\ntarget = '/x'"),
            "unknown key",
        )

    def test_invalid_exe_name_rejected(self):
        # the name becomes the systemd unit and the on-guest path
        self.assert_conf_error(
            VALID + '\n[exe."bad/name"]\nsource = "./x"',
            "invalid exe name",
        )

    def test_env_inline_map_misuse_mentions_env_vars(self):
        # A literal `env = { ... }` map alongside [exe.app.env.dev] is rejected
        # by TOML itself ("cannot declare twice") — which is exactly why app
        # vars use the 'env-vars' key. This checks the parser's hint for the
        # non-table form on an exe without env subtables.
        minimal = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[exe.app]
source = './bin/server'
env = { dev = 'oops' }
"""
        self.assert_conf_error(minimal, "env-vars")

    def test_bad_mem(self):
        self.assert_conf_error(VALID.replace("mem = '2gb'", "mem = '2 bananas'"), "invalid mem")

    def test_remote_env_with_vcpus_errors(self):
        # sizing is local-box-only; prod is remote
        self.assert_conf_error(
            VALID.replace(
                "vm = { anci-version = '0.0.1' }",
                "vm = { anci-version = '0.0.1', vcpus = 4 }",
            ),
            "vcpus/mem/disk",
        )

    def test_reserved_env_name_rejected(self):
        # `init` is an anci command, so it can't also be an env name
        minimal = """
[env.init]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'
"""
        self.assert_conf_error(minimal, "reserved")

    def test_reserved_exe_name_rejected(self):
        # `caddy`/`litestream` are anci's own units, so `<exe>.service` would clash
        for name in ("caddy", "litestream"):
            self.assert_conf_error(
                VALID + f"\n[exe.{name}]\nsource = './x'",
                "reserved",
            )

    def test_two_envs_on_one_box_rejected(self):
        dup = VALID + (
            "\n[env.staging]\nip = '203.0.113.10'\naccess-users = ['jack']\n"
            "mode = 'adopt'\nvm = { anci-version = '0.0.1' }\n"
        )
        self.assert_conf_error(dup, "same box")

    def test_missing_conf(self):
        with self.assertRaises(ConfError):
            load("/nonexistent/anci.toml")

    def test_invalid_toml(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("[user.jack\nbroken =")
            path = f.name
        with self.assertRaises(ConfError):
            load(path)


class TestModeAndVersion(unittest.TestCase):
    """`mode` (who manages the box) + top-level `anci-version`."""

    LOCAL = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
{env}
"""

    def test_top_level_anci_version(self):
        with mock.patch.object(validate.versions_mod, "load",
                               return_value={"0.0.1": None, "0.0.2": None}):
            conf = load_str("anci-version = '0.0.2'\n" + self.LOCAL.format(
                env="vm = { vcpus = 2, mem = '1gb', disk = '20gb' }"))
        self.assertEqual(conf.envs["dev"].anci_version, "0.0.2")

    def test_per_env_anci_version_overrides(self):
        with mock.patch.object(validate.versions_mod, "load",
                               return_value={"0.0.1": None, "0.0.2": None, "0.0.3": None}):
            conf = load_str("anci-version = '0.0.2'\n" + self.LOCAL.format(
                env="vm = { anci-version = '0.0.3', vcpus = 2, mem = '1gb', disk = '20gb' }"))
        self.assertEqual(conf.envs["dev"].anci_version, "0.0.3")

    def test_anci_version_is_required(self):
        with self.assertRaises(ConfError):
            load_str(self.LOCAL.format(env="vm = { vcpus = 2, mem = '1gb', disk = '20gb' }"))

    def test_managed_env_requires_vm(self):
        with self.assertRaises(ConfError):
            load_str(self.LOCAL.format(env=""))

    def test_managed_env_requires_sizing(self):
        with self.assertRaises(ConfError):
            load_str(self.LOCAL.format(env="vm = { anci-version = '0.0.1' }"))

    def test_adopt_env_makes_vm_optional(self):
        conf = load_str("anci-version = '0.0.1'\n"
                        + self.LOCAL.format(env="mode = 'adopt'")
                        .replace("127.0.0.1", "203.0.113.10"))
        self.assertTrue(conf.envs["dev"].adopted)

    def test_adopt_env_rejects_sizing(self):
        with self.assertRaises(ConfError):
            load_str(self.LOCAL.format(env="mode = 'adopt'\nvm = { vcpus = 2 }")
                     .replace("127.0.0.1", "203.0.113.10"))

    def test_mode_defaults_to_full(self):
        conf = load_str("anci-version = '0.0.1'\n" + self.LOCAL.format(
            env="vm = { vcpus = 2, mem = '1gb', disk = '20gb' }"))
        self.assertEqual(conf.envs["dev"].mode, "full")
        self.assertFalse(conf.envs["dev"].adopted)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ConfError) as cm:
            load_str(self.LOCAL.format(env="mode = 'banana'\nvm = { anci-version = '0.0.1' }"))
        self.assertIn("'full'", str(cm.exception))
        self.assertIn("'adopt'", str(cm.exception))

    def test_remote_managed_box_loads(self):
        # a non-local ip + mode = full = a box anci manages on a remote host
        conf = load_str("""
anci-version = '0.0.1'

[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.prod]
ip = '10.0.0.5'
access-users = ['jack']
vm = { vcpus = 4, mem = '8gb', disk = '40gb' }
""")
        env = conf.envs["prod"]
        self.assertEqual(env.ip, "10.0.0.5")
        self.assertFalse(env.adopted)
        self.assertEqual(env.vcpus, 4)

    def test_adopt_localhost_is_rejected(self):
        # would bootstrap the operator's own machine as the box
        with self.assertRaises(ConfError) as cm:
            load_str(self.LOCAL.format(env="mode = 'adopt'\nvm = { anci-version = '0.0.1' }"))
        self.assertIn("127.0.0.1", str(cm.exception))

    def test_names_that_reach_the_box_are_validated(self):
        import json

        def conf(user="jack", env="dev", exe="web.v2", db="main-db"):
            q = json.dumps   # a TOML basic string: valid as a key and a value
            return load_str(f"""
anci-version = '0.0.1'

[user.{q(user)}]
ssh-pub-keys = 'ssh-ed25519 AAAA k'

[env.{q(env)}]
ip = '127.0.0.1'
access-users = [{q(user)}]
vm = {{ vcpus = 2, mem = '1gb', disk = '20gb' }}

[exe.{q(exe)}]
source = './s'

[db.{q(db)}]
migrations = './m'
""")

        conf()   # the happy path still loads
        for field, bad in (("user", "Jack"), ("user", "o'neil"), ("user", "a b"),
                           ("env", ".."), ("db", "../etc"), ("db", "a b"),
                           ("exe", ".."), ("exe", "-x")):
            with self.subTest(**{field: bad}), self.assertRaises(ConfError) as cm:
                conf(**{field: bad})
            self.assertIn(f"invalid {field} name", str(cm.exception))

    def test_two_exes_cannot_share_a_host(self):
        with self.assertRaises(ConfError) as cm:
            load_str("""
anci-version = '0.0.1'

[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.prod]
ip = '203.0.113.10'
access-users = ['jack']
vm = { vcpus = 2, mem = '1gb', disk = '20gb' }

[exe.a]
source = './a'

[exe.a.env.prod]
host = 'x.example.com'

[exe.b]
source = './b'

[exe.b.env.prod]
host = 'x.example.com'
""")
        self.assertIn("already used", str(cm.exception))

    def _host_conf(self, env_ip: str, exe_block: str) -> str:
        return f"""
anci-version = '0.0.1'

[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '{env_ip}'
access-users = ['jack']
vm = {{ vcpus = 2, mem = '1gb', disk = '20gb' }}

[exe.a]
source = './a'
{exe_block}
"""

    def test_top_level_host_with_a_single_remote_env(self):
        conf = load_str(self._host_conf("203.0.113.10", "host = 'x.example.com'"))
        self.assertEqual(conf.exes["a"].resolve_for("dev").host, "x.example.com")
        conf = load_str(self._host_conf("203.0.113.10", "[exe.a.env.dev]\nhost = 'x.example.com'"))
        self.assertEqual(conf.exes["a"].resolve_for("dev").host, "x.example.com")

    def test_top_level_host_with_several_envs_is_set_per_env(self):
        two = self._host_conf("127.0.0.1", "host = 'x.example.com'").replace(
            "[exe.a]", "[env.prod]\nip = '203.0.113.10'\naccess-users = ['jack']\n"
                       "vm = { vcpus = 2, mem = '1gb', disk = '20gb' }\n\n[exe.a]")
        with self.assertRaises(ConfError) as cm:
            load_str(two)
        self.assertIn("'host' is set per env here", str(cm.exception))
        self.assertIn("[exe.a.env.prod] host = 'x.example.com'", cm.exception.try_)

    def test_top_level_host_with_a_single_local_env(self):
        with self.assertRaises(ConfError) as cm:
            load_str(self._host_conf("127.0.0.1", "host = 'x.example.com'"))
        self.assertIn("'host' is set per env here", str(cm.exception))

    def test_local_env_cant_have_a_host(self):
        with self.assertRaises(ConfError) as cm:
            load_str(self._host_conf("127.0.0.1", "[exe.a.env.dev]\nhost = 'x.example.com'"))
        self.assertIn("a local env can't have a 'host'", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
