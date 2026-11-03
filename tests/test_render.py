import json
import subprocess
import unittest

from anci.render import edge, fwd
from anci.render.cloudinit import render_userdata
from anci.render.systemd import render_oneshot, render_timer, render_unit


class TestRenderUnit(unittest.TestCase):
    def setUp(self):
        self.unit = render_unit(
            exe_name="main-app",
            env_name="dev",
            sysuser="anci",
            target="/var/lib/my-anci-app/server",
            args=["-config", "-blah"],
            port=8000,
            env_vars={"LOG_LEVEL": "debug", "GREETING": "hello dev"},
        )

    def test_shape(self):
        self.assertIn("[Unit]", self.unit)
        self.assertIn("Description=anci: main-app (dev)", self.unit)
        self.assertIn("User=anci", self.unit)
        self.assertIn("ExecStart=/var/lib/my-anci-app/server -config -blah", self.unit)
        self.assertIn("Environment=ANCI_ENV=dev", self.unit)
        self.assertIn("Environment=ANCI_BIND=127.0.0.1", self.unit)
        self.assertIn("Environment=ANCI_PORT=8000", self.unit)
        self.assertIn('Environment=GREETING="hello dev"', self.unit)
        self.assertIn("Environment=LOG_LEVEL=debug", self.unit)
        self.assertNotIn("EnvironmentFile=", self.unit)
        self.assertIn("Restart=on-failure", self.unit)
        self.assertIn("NoNewPrivileges=true", self.unit)
        self.assertIn("ProtectSystem=strict", self.unit)
        self.assertIn("WantedBy=multi-user.target", self.unit)

    def test_spacey_values_are_quoted(self):
        # regression: systemd splits Environment= on spaces — without quoting,
        # GREETING='hello from dev' arrived as just 'hello'
        unit = render_unit(
            exe_name="x", env_name="dev", sysuser="anci",
            target="/t", args=[], port=8000,
            env_vars={"GREETING": "hello from dev"},
        )
        self.assertIn('Environment=GREETING="hello from dev"', unit)
        self.assertNotIn("Environment=GREETING=hello from dev\n", unit)

    def test_percent_reaches_the_app_as_written(self):
        unit = render_unit(exe_name="a", env_name="dev", sysuser="anci", target="/t",
                           args=["--off=50%off", "${ANCI_PORT}"], port=8000,
                           env_vars={"PCT": "50%off"})
        self.assertIn("Environment=PCT=50%%off", unit)       # systemd turns %% back into %
        self.assertIn("--off=50%%off", unit)
        self.assertIn("'${ANCI_PORT}'", unit)                 # expansion still works

    def test_args_are_quoted(self):
        unit = render_unit(
            exe_name="x", env_name="dev", sysuser="anci",
            target="/t", args=["-msg", "two words"], port=8000, env_vars={},
        )
        self.assertIn("ExecStart=/t -msg 'two words'", unit)

    def test_no_args_no_trailing_space(self):
        unit = render_unit(
            exe_name="x", env_name="dev", sysuser="anci",
            target="/t", args=[], port=8000, env_vars={},
        )
        self.assertIn("ExecStart=/t\n", unit)

    def test_bind_is_configurable(self):
        unit = render_unit(
            exe_name="x", env_name="dev", sysuser="anci",
            target="/t", args=[], port=8000, env_vars={}, bind="0.0.0.0",
        )
        self.assertIn("Environment=ANCI_BIND=0.0.0.0", unit)

    def test_url_set_only_when_given(self):
        unit = render_unit(
            exe_name="x", env_name="dev", sysuser="anci",
            target="/t", args=[], port=8000, env_vars={}, url="https://app.example.com",
        )
        self.assertIn("Environment=ANCI_URL=https://app.example.com", unit)
        unit = render_unit(
            exe_name="x", env_name="dev", sysuser="anci",
            target="/t", args=[], port=8000, env_vars={},
        )
        self.assertNotIn("ANCI_URL", unit)

    def test_log_file_appended(self):
        unit = render_unit(
            exe_name="x", env_name="dev", sysuser="anci",
            target="/t", args=[], port=8000, env_vars={},
            log_file="/var/log/anci/x.log",
        )
        self.assertIn("StandardOutput=append:/var/log/anci/x.log", unit)
        self.assertIn("StandardError=append:/var/log/anci/x.log", unit)
        # ProtectSystem=strict would otherwise mount /var read-only
        self.assertIn("ReadWritePaths=-/var/log/anci", unit)

    def test_db_dirs_are_the_only_write_paths(self):
        # the app may write only its db dirs (state), never the managed root
        unit = render_unit(
            exe_name="x", env_name="dev", sysuser="anci",
            target="/var/lib/anci/exe/x/bin", args=[], port=8000, env_vars={},
            rw_paths=["/var/lib/anci/db/main-db"],
        )
        self.assertIn("ReadWritePaths=/var/lib/anci/db/main-db", unit)
        self.assertNotIn("ReadWritePaths=/var/lib/anci\n", unit)

    def test_no_log_file_no_append(self):
        self.assertNotIn("StandardOutput=", self.unit)
        self.assertNotIn("StandardError=", self.unit)


class TestRenderOneshot(unittest.TestCase):
    def test_shape(self):
        unit = render_oneshot(
            exe_name="cleanup-job", env_name="dev", sysuser="anci",
            target="/var/lib/x/cleanup", args=["--dry"],
            env_vars={"LOG_LEVEL": "info"},
        )
        self.assertIn("[Unit]", unit)
        self.assertIn("Description=anci: cleanup-job (dev)", unit)
        self.assertIn("StartLimitIntervalSec=0", unit)   # allow sub-2s schedules
        self.assertIn("Type=oneshot", unit)
        self.assertIn("User=anci", unit)
        self.assertIn("ExecStart=/var/lib/x/cleanup --dry", unit)
        self.assertIn("Environment=ANCI_ENV=dev", unit)
        self.assertIn("Environment=LOG_LEVEL=info", unit)
        self.assertIn("ProtectSystem=strict", unit)  # hardening still applies

    def test_no_port_no_restart_no_install(self):
        unit = render_oneshot(
            exe_name="x", env_name="dev", sysuser="anci",
            target="/t", args=[], env_vars={},
        )
        self.assertNotIn("ANCI_PORT", unit)
        self.assertNotIn("ANCI_URL", unit)
        self.assertNotIn("Restart=", unit)
        self.assertNotIn("[Install]", unit)

    def test_optional_env_file(self):
        unit = render_oneshot(
            exe_name="x", env_name="dev", sysuser="anci",
            target="/t", args=[], env_vars={}, env_file="/etc/anci/secrets/dev.env",
        )
        self.assertIn("EnvironmentFile=-/etc/anci/secrets/dev.env", unit)

    def test_log_file_appended(self):
        unit = render_oneshot(
            exe_name="job", env_name="dev", sysuser="anci",
            target="/t", args=[], env_vars={},
            log_file="/var/log/anci/job.log",
        )
        self.assertIn("StandardOutput=append:/var/log/anci/job.log", unit)
        self.assertIn("StandardError=append:/var/log/anci/job.log", unit)


class TestRenderTimer(unittest.TestCase):
    def test_shape(self):
        timer = render_timer(exe_name="cleanup-job", env_name="dev", schedule="daily")
        self.assertIn("[Timer]", timer)
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("AccuracySec=1s", timer)   # default 1min makes sub-minute timers erratic
        self.assertIn("Persistent=true", timer)
        self.assertIn("[Install]", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_schedule_passes_through(self):
        timer = render_timer(exe_name="x", env_name="dev", schedule="*-*-* 03:00:00")
        self.assertIn("OnCalendar=*-*-* 03:00:00", timer)


class TestRenderUserdata(unittest.TestCase):
    def test_renders_one_account_per_user(self):
        ud = render_userdata(
            {
                "jack": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI5064a8d508da9f415ed7476ad71d5b7a03a1605098c a", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIe4a3381881dbfac98b3d0f3d53f63f005d0bf3268cb b"],
                "alice": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI41985beffbeaf4229dd70403c10b376c558534e0bad c"],
            }
        )
        self.assertTrue(ud.startswith("#cloud-config\n"))
        doc = json.loads(ud.split("\n", 1)[1])   # JSON is valid YAML
        users = {u["name"]: u for u in doc["users"]}
        self.assertEqual(users["jack"]["ssh_authorized_keys"],
                         ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI5064a8d508da9f415ed7476ad71d5b7a03a1605098c a", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIe4a3381881dbfac98b3d0f3d53f63f005d0bf3268cb b"])
        self.assertEqual(users["alice"]["ssh_authorized_keys"], ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI41985beffbeaf4229dd70403c10b376c558534e0bad c"])
        self.assertEqual(users["jack"]["groups"], ["anci"])
        # sudo through the file anci's user resource owns (and revokes), not
        # cloud-init's `sudo:` key, whose sudoers file anci never sees
        self.assertNotIn("sudo", users["jack"])
        grant = next(f for f in doc["write_files"] if f["path"] == "/etc/sudoers.d/anci-jack")
        self.assertEqual(grant["content"], "jack ALL=(ALL) NOPASSWD:ALL\n")
        self.assertEqual(grant["permissions"], "0440")
        self.assertIs(doc["ssh_pwauth"], False)

    def test_key_comments_survive(self):
        # hand-built YAML once cut a key at " #" (a YAML comment); JSON can't
        ud = render_userdata({"jack": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI72ba82c46560c47f070c1ada767277bc694dea48fd1 k jack@dev-machine # work: main"]})
        doc = json.loads(ud.split("\n", 1)[1])
        self.assertEqual(doc["users"][0]["ssh_authorized_keys"],
                         ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI72ba82c46560c47f070c1ada767277bc694dea48fd1 k jack@dev-machine # work: main"])

    def test_refuses_empty(self):
        with self.assertRaises(ValueError):
            render_userdata({})


class TestLibvirtHooks(unittest.TestCase):
    """The edge and the localhost forward share one hook and POSIX-sh apply scripts."""

    def test_hook_runs_its_script_through_sh(self):
        # Ubuntu's AppArmor profile for libvirtd denies a hook exec'ing /usr/local/libexec
        for mod in (edge, fwd):
            hook = mod.render_hook()
            self.assertIn(f"/bin/sh {mod.APPLY_SCRIPT} ", hook)
            self.assertEqual(hook.replace(mod.APPLY_SCRIPT, "X"),
                             fwd.render_hook().replace(fwd.APPLY_SCRIPT, "X"))

    def test_scripts_are_posix_sh(self):
        for script in (edge.render_apply_script(), fwd.render_apply_script(), fwd.render_hook()):
            self.assertTrue(script.startswith("#!/bin/sh\n"))
            subprocess.run(["sh", "-n"], input=script, text=True, check=True)


if __name__ == "__main__":
    unittest.main()
