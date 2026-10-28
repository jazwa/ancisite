"""libvirt provisioning (no box): the Hypervisor transport, domain state, up /
down / recreate, VM resize, the address registry, and remote-host ssh routing.

Everything runs against `FakeHypervisor`, which records the root scripts and
virsh calls a verb issues and answers them from small tables.
"""

import contextlib
import io
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anci import errors, ssh, ui
from anci.box import ssh_hint
from anci.conf import Env, load
from anci import hypervisor as hv_mod
from anci.providers import libvirt as lvm

DUMPXML = """<domain type='kvm'>
  <name>anci-dev</name>
  <memory unit='KiB'>1048576</memory>
  <currentMemory unit='KiB'>1048576</currentMemory>
  <vcpu placement='static' current='2'>4</vcpu>
  <devices>
    <disk type='file' device='disk'>
      <source file='/var/lib/libvirt/images/anci-dev.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
  </devices>
</domain>
"""


class FakeHypervisor(lvm.Hypervisor):
    """Records `root` scripts and virsh argv; answers from `virsh_out` / `fail`.

    `states` is the sequence `virsh domstate` walks through (last one sticks);
    None = the domain doesn't exist.
    """

    def __init__(self, login=None, *, states=("running",), virsh_out=None, fail=()):
        super().__init__(login)
        self.states = list(states)
        self.virsh_out = virsh_out or {}
        self.fail = set(fail)          # virsh verbs / script substrings that fail
        self.scripts: list[tuple[str, str | None]] = []
        self.calls: list[tuple[str, ...]] = []

    def run(self, script, *, input=None):
        if script.startswith("virsh --connect qemu:///system --quiet "):
            args = tuple(shlex.split(script)[4:])
            self.calls.append(args)
            return self._virsh(args)
        self.scripts.append((script, input))
        rc = 1 if any(f in script for f in self.fail) else 0
        return subprocess.CompletedProcess([], rc, "cached\n" if "curl" in script else "", "")

    def _virsh(self, args):
        verb = args[0]
        if verb in self.fail or (verb == "domstate" and self.states[0] is None) or \
                (verb == "dominfo" and self.states[0] is None):
            return subprocess.CompletedProcess([], 1, "", "error")
        if verb == "domstate":
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return subprocess.CompletedProcess([], 0, state + "\n", "")
        return subprocess.CompletedProcess([], 0, self.virsh_out.get(verb, ""), "")

    def verbs(self):
        return [c[0] for c in self.calls]


CONF = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '{ip}'
access-users = ['jack']
vm = {{ anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }}
"""


def _conf(ip="127.0.0.1"):
    d = Path(tempfile.mkdtemp())
    (d / "anci.toml").write_text(CONF.format(ip=ip))
    return load(str(d / "anci.toml"))


def _dom(conf, env="dev"):
    return lvm.net.domain_name(lvm.paths.box_id(conf), env)


def _env(**kw) -> Env:
    base = dict(name="dev", ip="127.0.0.1", access_users=["jack"],
                anci_version="0.0.1", vcpus=2, mem="1gb", disk="20gb")
    base.update(kw)
    return Env(**base)


def _facts(**kw) -> lvm.DomainFacts:
    base = dict(running=True, vcpus=2, max_vcpus=2, mem_kib=1024 * 1024,
                max_mem_kib=1024 * 1024, disk_path="/d.qcow2", disk_bytes=20 * 1024 ** 3)
    base.update(kw)
    return lvm.DomainFacts(**base)


@contextlib.contextmanager
def _with(hv, *, version_sha=None):
    """Route `hypervisor()` (and `libvirt_hypervisor()`) to `hv` and stub the
    version map + artifacts dir."""
    with mock.patch.object(lvm, "hypervisor", return_value=hv), \
         mock.patch.object(lvm, "libvirt_hypervisor", return_value=hv), \
         mock.patch.object(lvm, "versions_mod") as vmod, \
         mock.patch.object(lvm.time, "sleep"), \
         contextlib.redirect_stdout(io.StringIO()) as out:
        vmod.get.return_value = mock.Mock(
            debian_image_url="https://x/debian.qcow2", sha512=version_sha)
        yield out


# ---------------------------------------------------------------------------
# the transport


class TestHypervisorTransport(unittest.TestCase):
    def test_local_runs_under_sudo(self):
        self.assertEqual(hv_mod.Hypervisor()._argv("true"), ["sudo", "-n", "bash", "-c", "true"])

    def test_remote_runs_over_shared_ssh_with_sudo(self):
        argv = hv_mod.Hypervisor("jack@10.0.0.5")._argv("echo hi")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("ControlMaster=auto", argv)
        self.assertIn("BatchMode=yes", argv)
        self.assertEqual(argv[-2:], ["jack@10.0.0.5", "sudo -n bash -c 'echo hi'"])

    def test_virsh_targets_the_system_daemon(self):
        hv = FakeHypervisor()
        hv.virsh("list", "--all")
        self.assertEqual(hv.calls, [("list", "--all")])

    def test_sudo_password_prompt_is_explained(self):
        hv = hv_mod.Hypervisor("jack@10.0.0.5")
        p = subprocess.CompletedProcess([], 1, "", "sudo: a password is required")
        err = hv.failure(p, "x")
        self.assertIn("passwordless sudo", err.try_)

    def test_unreachable_remote_is_explained(self):
        err = hv_mod.Hypervisor("jack@10.0.0.5").failure(
            subprocess.CompletedProcess([], 255, "", "no route"), "x")
        self.assertIn("can't ssh to jack@10.0.0.5", str(err))

    def test_remote_hypervisor_needs_no_local_sudo(self):
        conf = _conf("10.0.0.5")
        with mock.patch.object(hv_mod.auth, "authorize", return_value="jack"), \
             mock.patch.object(hv_mod.subprocess, "run") as run:
            hv = lvm.hypervisor(conf, "dev")
        self.assertEqual(hv.login, "jack@10.0.0.5")
        run.assert_not_called()

    def test_local_hypervisor_asks_sudo_once(self):
        with mock.patch.object(hv_mod.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0)) as run:
            hv = hv_mod.hypervisor(_conf(), "dev")
        self.assertIsNone(hv.login)
        run.assert_called_once_with(["sudo", "-v"])

    def test_libvirt_group_member_runs_virsh_without_sudo(self):
        ok = subprocess.CompletedProcess([], 0, "qemu:///system\n", "")
        with mock.patch.object(hv_mod.subprocess, "run", return_value=ok) as run:
            hv = hv_mod.libvirt_hypervisor(_conf(), "dev")
        self.assertFalse(hv.sudo)
        self.assertNotIn(["sudo", "-v"], [c.args[0] for c in run.call_args_list])

    def test_libvirt_refusing_you_falls_back_to_sudo(self):
        refused = subprocess.CompletedProcess([], 1, "", "permission denied")
        ok = subprocess.CompletedProcess([], 0)
        with mock.patch.object(hv_mod.subprocess, "run", side_effect=[refused, ok]) as run:
            hv = hv_mod.libvirt_hypervisor(_conf(), "dev")
        self.assertTrue(hv.sudo)
        self.assertEqual(run.call_args_list[-1].args[0], ["sudo", "-v"])

    def test_libvirt_hypervisor_for_a_remote_host_is_the_usual_one(self):
        conf = _conf("10.0.0.5")
        with mock.patch.object(hv_mod.auth, "authorize", return_value="jack"), \
             mock.patch.object(hv_mod.subprocess, "run") as run:
            hv = hv_mod.libvirt_hypervisor(conf, "dev")
        self.assertEqual(hv.login, "jack@10.0.0.5")
        run.assert_not_called()


# ---------------------------------------------------------------------------
# domain state


class TestDomState(unittest.TestCase):
    def test_running_is_exact_match(self):
        self.assertTrue(lvm._domain_running(FakeHypervisor(states=["running"]), "d"))

    def test_paused_is_not_running(self):
        self.assertFalse(lvm._domain_running(FakeHypervisor(states=["paused"]), "d"))

    def test_absent_domain(self):
        hv = FakeHypervisor(states=[None])
        self.assertIsNone(lvm._domstate(hv, "d"))
        self.assertFalse(lvm._domain_exists(hv, "d"))

    def test_daemon_down_is_its_own_error(self):
        with self.assertRaises(errors.EnvError) as cm:
            lvm._require_daemon(FakeHypervisor(fail={"uri"}))
        self.assertIn("libvirt daemon", str(cm.exception))
        with self.assertRaises(errors.EnvError) as cm:
            lvm._require_daemon(FakeHypervisor("jack@10.0.0.5", fail={"uri"}))
        self.assertIn("mode = 'adopt'", cm.exception.try_)   # the likeliest misconfig


class TestStopDomain(unittest.TestCase):
    def test_graceful_shutdown_no_destroy(self):
        hv = FakeHypervisor(states=["running", "running", "shut off"])
        with mock.patch.object(lvm.time, "sleep"):
            self.assertFalse(lvm._stop_domain(hv, "d"))
        self.assertIn("shutdown", hv.verbs())
        self.assertNotIn("destroy", hv.verbs())

    def test_hung_guest_falls_back_to_hard_poweroff(self):
        hv = FakeHypervisor(states=["running"])
        with mock.patch.object(lvm.time, "monotonic", side_effect=[t * 100 for t in range(50)]), \
             mock.patch.object(lvm.time, "sleep"):
            self.assertTrue(lvm._stop_domain(hv, "d"))
        self.assertIn("destroy", hv.verbs())


# ---------------------------------------------------------------------------
# up / down


class TestEnvDown(unittest.TestCase):
    def test_graceful_stop(self):
        conf = _conf()
        hv = FakeHypervisor(states=["running", "running", "shut off"])
        with _with(hv) as out:
            lvm.env_down(conf, "dev")
        self.assertIn(f"stopped {_dom(conf)} (disk kept)", out.getvalue())
        self.assertNotIn("FORCED", out.getvalue())

    def test_hard_poweroff_warns_in_caps(self):
        conf = _conf()
        hv = FakeHypervisor(states=["running"])
        with _with(hv) as out, \
             mock.patch.object(lvm, "_stop_domain", return_value=True), \
             mock.patch.object(lvm, "_domain_running", side_effect=[True, False]):
            lvm.env_down(conf, "dev")
        self.assertIn("FORCED power-off", out.getvalue())

    def test_no_box_is_idempotent(self):
        with _with(FakeHypervisor(states=[None])) as out:
            lvm.env_down(_conf(), "dev")
        self.assertIn("nothing to stop", out.getvalue())

    def test_adopt_env_is_refused(self):
        conf = _conf("203.0.113.10")
        conf.envs["dev"].mode = "adopt"
        with self.assertRaises(errors.ConfError):
            lvm.env_down(conf, "dev")


class TestEnvUp(unittest.TestCase):
    def _up(self, hv, conf=None, **kw):
        conf = conf or _conf()
        # resize has its own tests; here it's a no-op
        with _with(hv, version_sha="abc") as out, \
             mock.patch.object(lvm, "_reconcile_domain", return_value=(False, "ok")):
            ip = lvm.env_up(conf, "dev", **kw)
        return conf, ip, out.getvalue()

    def test_fresh_box_is_provisioned_on_the_hypervisor(self):
        hv = FakeHypervisor(states=[None])
        conf, ip, _ = self._up(hv)
        self.assertTrue(ip.startswith("192.168.122."))
        scripts = "\n".join(s for s, _ in hv.scripts)
        self.assertIn("curl -fsSL", scripts)
        self.assertIn("sha512sum", scripts)
        self.assertIn("cp --reflink=auto", scripts)
        self.assertIn("qemu-img resize", scripts)
        install = next(s for s, _ in hv.scripts if s.startswith("virt-install"))
        self.assertIn("--cloud-init", install)
        self.assertIn(lvm.CLOUDINIT_DIR, install)
        cloud = [i for s, i in hv.scripts if lvm.CLOUDINIT_DIR in s and i]
        self.assertEqual(len(cloud), 2)                     # user-data + network-config
        self.assertIn(f"/dev/tcp/{ip}/22", hv.scripts[-1][0])  # readiness, from the hypervisor

    def test_stopped_box_is_started(self):
        hv = FakeHypervisor(states=["shut off"])
        _, _, text = self._up(hv)
        self.assertIn("start", hv.verbs())
        self.assertNotIn("WARNING", text)
        self.assertFalse(any(s.startswith("virt-install") for s, _ in hv.scripts))

    def test_recreate_force_destroys_and_rebuilds(self):
        hv = FakeHypervisor(states=["running"])
        conf, _, text = self._up(hv, recreate=True, force=True)
        self.assertIn("IT DELETES THE BOX'S DISK", text)
        self.assertIn("undefine", hv.verbs())
        self.assertTrue(any(s.startswith("rm -f") and _dom(conf) in s for s, _ in hv.scripts))
        self.assertTrue(any(s.startswith("virt-install") for s, _ in hv.scripts))

    def test_recreate_on_a_remote_host_drops_the_stale_pin(self):
        conf = _conf("10.0.0.5")
        pin = lvm.pin_path(conf, "dev")
        pin.parent.mkdir(parents=True)
        pin.write_text("192.168.122.9 ssh-ed25519 OLD\n")
        _, _, text = self._up(FakeHypervisor("jack@10.0.0.5", states=["running"]),
                              conf, recreate=True, force=True)
        self.assertFalse(pin.exists())
        self.assertIn("commit the new envs/dev/hostkey", text)

    def test_plain_up_keeps_the_pin(self):
        conf = _conf("10.0.0.5")
        pin = lvm.pin_path(conf, "dev")
        pin.parent.mkdir(parents=True)
        pin.write_text("k\n")
        self._up(FakeHypervisor("jack@10.0.0.5", states=["running"]), conf)
        self.assertTrue(pin.exists())

    def test_recreate_answered_no_aborts(self):
        hv = FakeHypervisor(states=["running"])
        with mock.patch.object(ui, "sys") as sysmod, mock.patch("builtins.input", return_value="n"):
            sysmod.stdin.isatty = lambda: True
            _, ip, text = self._up(hv, recreate=True)
        self.assertIsNone(ip)
        self.assertIn("aborted — nothing changed", text)
        self.assertNotIn("undefine", hv.verbs())

    def test_recreate_without_tty_or_force_refuses(self):
        hv = FakeHypervisor(states=["running"])
        with mock.patch.object(ui, "sys") as sysmod:
            sysmod.stdin.isatty = lambda: False
            with self.assertRaises(errors.EnvError) as cm:
                self._up(hv, recreate=True)
        self.assertIn("--force", cm.exception.try_)
        self.assertNotIn("undefine", hv.verbs())


# ---------------------------------------------------------------------------
# resize


class TestResize(unittest.TestCase):
    def test_domain_facts(self):
        hv = FakeHypervisor(virsh_out={"dumpxml": DUMPXML,
                                       "domblkinfo": "Capacity:       21474836480\n"})
        facts = lvm._domain_facts(hv, "anci-dev")
        self.assertTrue(facts.running)
        self.assertEqual((facts.vcpus, facts.max_vcpus), (2, 4))
        self.assertEqual((facts.mem_kib, facts.max_mem_kib), (1048576, 1048576))
        self.assertEqual(facts.disk_path, "/var/lib/libvirt/images/anci-dev.qcow2")
        self.assertEqual(facts.disk_bytes, 20 * 1024 ** 3)

    def test_stopped_disk_size_reads_qemu_img(self):
        hv = FakeHypervisor()
        hv.run = lambda script, input=None: subprocess.CompletedProcess(
            [], 0, '{"virtual-size": 10737418240}', "")
        self.assertEqual(lvm._disk_capacity(hv, "d", "/d.qcow2", running=False), 10737418240)

    def test_plan_noop_grow_and_max(self):
        self.assertFalse(lvm._resize_plan(_env(), _facts()).changed)
        plan = lvm._resize_plan(_env(vcpus=4, mem="2gb"),
                                _facts(max_vcpus=4, max_mem_kib=4 * 1024 * 1024))
        self.assertEqual((plan.vcpus, plan.mem_kib, plan.notes), (4, 2 * 1024 * 1024, []))
        plan = lvm._resize_plan(_env(vcpus=8, mem="2gb"), _facts())
        self.assertEqual((plan.vcpus_max, plan.mem_max_kib), (8, 2 * 1024 * 1024))
        self.assertTrue(plan.vcpus_config and plan.mem_config)
        self.assertEqual(lvm._resize_plan(_env(disk="40gb"), _facts()).disk_bytes, 40 * 1024 ** 3)

    def test_disk_shrink_is_a_hard_error(self):
        with self.assertRaises(errors.EnvError) as cm:
            lvm._resize_plan(_env(disk="10gb"), _facts())
        self.assertIn("shrink", str(cm.exception))

    def test_apply_live_while_running(self):
        hv = FakeHypervisor()
        lvm._apply_resize(hv, "d", lvm.ResizePlan(vcpus=4, mem_kib=2048, disk_bytes=99),
                          _facts(running=True))
        self.assertIn(("setvcpus", "d", "4", "--live", "--config"), hv.calls)
        self.assertIn(("setmem", "d", "2048", "--live", "--config"), hv.calls)
        # in bytes: virsh reads a bare number as KiB (22 GiB became 22 TiB once)
        self.assertIn(("blockresize", "d", "/d.qcow2", "99B"), hv.calls)

    def test_apply_stopped_is_config_only_and_grows_the_file(self):
        hv = FakeHypervisor()
        lvm._apply_resize(hv, "d", lvm.ResizePlan(vcpus=4, disk_bytes=99), _facts(running=False))
        self.assertIn(("setvcpus", "d", "4", "--config"), hv.calls)
        self.assertEqual(hv.scripts, [("qemu-img resize /d.qcow2 99", None)])

    def test_raised_max_is_config_only(self):
        hv = FakeHypervisor()
        lvm._apply_resize(hv, "d", lvm.ResizePlan(vcpus=8, vcpus_max=8, vcpus_config=True),
                          _facts(running=True))
        self.assertIn(("setvcpus", "d", "8", "--maximum", "--config"), hv.calls)
        self.assertIn(("setvcpus", "d", "8", "--config"), hv.calls)

    def test_live_unsupported_falls_back_to_config(self):
        hv = FakeHypervisor()
        real = hv._virsh
        hv._virsh = lambda args: (subprocess.CompletedProcess([], 1, "", "nope")
                                  if "--live" in args else real(args))
        plan = lvm.ResizePlan(vcpus=1)
        lvm._apply_resize(hv, "d", plan, _facts(running=True))
        self.assertIn(("setvcpus", "d", "1", "--config"), hv.calls)
        self.assertTrue(any("next restart" in n for n in plan.notes))


class TestVmReconcile(unittest.TestCase):
    def test_adopt_is_a_noop(self):
        conf = _conf("203.0.113.10")
        conf.envs["dev"].mode = "adopt"
        with mock.patch.object(lvm, "hypervisor") as hvf, \
             mock.patch.object(lvm, "libvirt_hypervisor") as lhvf:
            lvm.vm_reconcile(conf, "dev")
        hvf.assert_not_called()
        lhvf.assert_not_called()

    def test_resizes_through_libvirt_alone(self):
        # only virsh: no root scripts, so no sudo for a libvirt group member
        hv = FakeHypervisor(states=["running"], virsh_out={
            "dumpxml": DUMPXML, "domblkinfo": f"Capacity: {20 * 1024 ** 3}\n"})
        conf = _conf()
        conf.envs["dev"].vcpus = 3
        with _with(hv):
            lvm.vm_reconcile(conf, "dev")
        self.assertEqual(hv.scripts, [])
        self.assertIn(("setvcpus", _dom(conf), "3", "--live", "--config"), hv.calls)

    def test_running_box_is_resized(self):
        hv = FakeHypervisor(states=["running"])
        with _with(hv), mock.patch.object(lvm, "_reconcile_domain",
                                          return_value=(True, "4 vcpus")) as rec:
            lvm.vm_reconcile(_conf(), "dev")
        rec.assert_called_once()

    def test_stopped_or_absent_box_fails_fast(self):
        for states, words in ((["shut off"], "shut off"), ([None], "no box")):
            with _with(FakeHypervisor(states=states)), \
                 self.assertRaises(errors.EnvError) as cm:
                lvm.vm_reconcile(_conf(), "dev")
            self.assertIn(words, str(cm.exception))
            self.assertIn("anci dev up", cm.exception.try_)


# ---------------------------------------------------------------------------
# registry


class TestRegistry(unittest.TestCase):
    def test_parses_descriptions(self):
        hv = FakeHypervisor(virsh_out={
            "list": "anci-proj-1-dev\nother\n",
            "dumpxml": "<domain><description>anci box_id=proj-1 env=dev "
                       "ip=192.168.122.50</description></domain>"})
        self.assertEqual(lvm._registry(hv), {"192.168.122.50": "proj-1/dev"})

    def test_collision_is_a_hard_error(self):
        with mock.patch.object(lvm, "_registry", return_value={"192.168.122.50": "other-9/dev"}):
            with self.assertRaises(errors.EnvError) as cm:
                lvm._claim_or_fail(FakeHypervisor(), _conf(), "dev", "192.168.122.50")
        self.assertIn("already claimed", str(cm.exception))

    def test_own_claim_is_fine(self):
        conf = _conf()
        owner = f"{lvm.paths.box_id(conf)}/dev"
        with mock.patch.object(lvm, "_registry", return_value={"192.168.122.50": owner}):
            lvm._claim_or_fail(FakeHypervisor(), conf, "dev", "192.168.122.50")


# ---------------------------------------------------------------------------
# a box on a remote host is reached through it


class TestRemoteHostRouting(unittest.TestCase):
    def _conn(self, conf):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict("os.environ", {"XDG_RUNTIME_DIR": td}), \
             contextlib.redirect_stdout(io.StringIO()):
            pin = Path(td) / "hostkey"   # an adopted box is only reachable once pinned
            pin.write_text("k\n")
            with mock.patch.object(ssh, "pin_path", return_value=pin):
                return ssh.connect(conf, "dev", "jack")

    def test_managed_remote_jumps_through_the_host(self):
        conn = self._conn(_conf("10.0.0.5"))
        self.assertIn("ProxyJump=jack@10.0.0.5", conn.opts)
        self.assertTrue(conn.ip.startswith("192.168.122."))  # the VM, not the host

    def test_local_and_adopt_do_not_jump(self):
        ext = _conf("203.0.113.10")
        ext.envs["dev"].mode = "adopt"
        for conf in (_conf(), ext):
            self.assertFalse(any(o.startswith("ProxyJump") for o in self._conn(conf).opts))

    def test_ssh_hint_jumps(self):
        self.assertTrue(ssh_hint(_conf("10.0.0.5"), "dev", "jack")
                        .startswith("ssh -J jack@10.0.0.5 jack@192.168.122."))


if __name__ == "__main__":
    unittest.main()


class TestRequireRunning(unittest.TestCase):
    """`migrate`'s box check: one read-only `virsh domstate`, never a resize."""

    def _check(self, rc, out="", err="", ip="127.0.0.1"):
        p = subprocess.CompletedProcess([], rc, stdout=out, stderr=err)
        with mock.patch.object(lvm.subprocess, "run", return_value=p) as run, \
             mock.patch.object(lvm, "hypervisor") as hv, \
             mock.patch.object(lvm, "_reconcile_domain") as resize:
            lvm.require_running(_conf(ip), "dev")
        resize.assert_not_called()
        hv.assert_not_called()                 # locally: no sudo, no hypervisor transport
        self.assertEqual(run.call_args.args[0][:4], ["virsh", "--connect", "qemu:///system", "--quiet"])
        self.assertNotIn("sudo", run.call_args.args[0])

    def test_running_passes(self):
        self._check(0, "running\n")

    def test_stopped_box_is_refused(self):
        with self.assertRaises(errors.EnvError) as cm:
            self._check(0, "shut off\n")
        self.assertIn("shut off, not running", str(cm.exception))

    def test_missing_box_is_refused(self):
        with self.assertRaises(errors.EnvError) as cm:
            self._check(1, err="error: failed to get domain 'anci-x-dev'\n")
        self.assertIn("no box for dev", str(cm.exception))

    def test_unreadable_state_leaves_it_to_ssh(self):
        self._check(1, err="error: authentication unavailable: no polkit agent\n")

    def test_adopt_box_is_not_checked(self):
        conf = _conf("203.0.113.10")
        conf.envs["dev"].mode = "adopt"
        with mock.patch.object(lvm.subprocess, "run") as run:
            lvm.require_running(conf, "dev")
        run.assert_not_called()


class TestVmDifferences(unittest.TestCase):
    """`plan`'s VM line: read as you, never through sudo on this machine."""

    def _diff(self, conf, facts=None, raises=None):
        from anci.providers import libvirt as lv
        with mock.patch.object(lv, "_domain_facts", side_effect=raises, return_value=facts) as read:
            out = lv.vm_differences(conf, "dev")
        return out, read

    def _conf(self, **vm):
        conf = mock.Mock()
        env = mock.Mock(adopted=False, ip="127.0.0.1", **{"vcpus": 2, "mem": "2gb", "disk": "20gb", **vm})
        env.name = "dev"
        conf.envs = {"dev": env}
        return conf

    def test_reads_without_sudo_and_names_what_differs(self):
        from anci.providers import libvirt as lv
        facts = lv.DomainFacts(running=True, vcpus=2, max_vcpus=2, mem_kib=2 * 1024 ** 2,
                               max_mem_kib=2 * 1024 ** 2, disk_path="/d", disk_bytes=20 * 1024 ** 3)
        with mock.patch.object(lv.paths, "box_id", return_value="b"):
            out, read = self._diff(self._conf(vcpus=4, disk="40gb"), facts=facts)
        self.assertFalse(read.call_args.args[0].sudo)
        self.assertEqual(out, {"vcpus": ("2", "4"), "disk": ("20 GiB", "40 GiB")})

    def test_nothing_when_libvirt_refuses(self):
        from anci import errors
        with mock.patch("anci.providers.libvirt.paths.box_id", return_value="b"):
            out, _ = self._diff(self._conf(), raises=errors.EnvError("permission denied"))
        self.assertEqual(out, {})
