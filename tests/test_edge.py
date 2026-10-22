"""The DNAT edge on a remote hypervisor host (no box): render + reconcile."""

import tempfile
import unittest
from unittest import mock

from anci.render import caddy
from anci import edge, errors, net, paths
from anci.conf import allocate_ports, load


def _conf(ip: str = "10.0.0.5", host: bool = True):
    fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False,
                                     dir=tempfile.mkdtemp())
    exe_host = "[exe.web.env.prod]\nhost = 'app.example.com'" if host else ""
    fd.write(f"""
anci-version = '0.0.1'

[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.prod]
ip = '{ip}'
access-users = ['jack']
vm = {{ vcpus = 2, mem = '1gb', disk = '20gb' }}

[exe.web]
source = 'target/web'
{exe_host}
""")
    fd.close()
    return load(fd.name)


class FakeHost:
    """The host's Hypervisor: answers the probe, records root scripts."""

    def __init__(self, route: str = "", shas: dict | None = None):
        self.route, self.shas = route, shas or {}
        self.scripts: list[tuple[str, str | None]] = []
        self.probes: list[dict] = []

    def __call__(self, login):
        assert login == "jack@10.0.0.5"
        return self

    def probe(self, probes):
        self.probes.append(probes)
        found = {p: [sha] for p, sha in self.shas.items() if p in probes}
        if self.route:
            found["route"] = self.route.splitlines()
        return found

    def root(self, script, what, *, input=None):
        self.scripts.append((script, input))
        return ""

    def virsh(self, *args, check=True):
        return mock.Mock(stdout="Bridge:         virbr0\n")

    def text(self) -> str:
        return "\n".join(s for s, _ in self.scripts)


def _reconcile(conf, fake):
    with mock.patch.object(edge, "Hypervisor", side_effect=fake):
        return edge.reconcile(conf, "prod", "jack")


class TestRender(unittest.TestCase):
    def test_ruleset(self):
        r = edge.render_ruleset("192.168.122.57")
        self.assertIn("table ip anci_edge", r)
        self.assertIn("delete table ip anci_edge", r)  # atomic replace
        self.assertIn("tcp dport { 80, 443 } dnat to 192.168.122.57", r)
        self.assertIn("udp dport 443 dnat to 192.168.122.57", r)
        self.assertIn("ip daddr != 127.0.0.0/8", r)  # hairpin spares loopback

    def test_apply_script_handles_both_libvirt_backends(self):
        s = edge.render_apply_script()
        self.assertIn("ct status dnat accept", s)
        self.assertIn("--ctstate DNAT", s)
        self.assertNotIn("virsh", s)  # runs inside libvirt's hook

    def test_caddyfile_for_remote_uses_acme_without_proxy_protocol(self):
        conf = _conf()
        text = caddy.caddyfile_for(conf, "prod", allocate_ports(conf))
        self.assertIn("app.example.com {", text)
        self.assertNotIn("proxy_protocol", text)
        self.assertNotIn("tls ", text)   # Let's Encrypt


class TestReconcile(unittest.TestCase):
    def test_fresh_host_writes_everything_and_applies(self):
        conf = _conf()
        fake = FakeHost()
        detail = _reconcile(conf, fake)
        vm_ip = net.env_ip(paths.box_id(conf), "prod")
        self.assertEqual(detail, f"80/443 → {vm_ip}")
        written = {s: i for s, i in fake.scripts if i is not None}
        self.assertEqual(len(written), 4)
        route = next(i for s, i in written.items() if edge.ROUTE_FILE in s)
        self.assertIn(f"OWNER={paths.box_id(conf)}/prod", route)
        self.assertIn(f"IP={vm_ip}", route)
        self.assertIn("BRIDGE=virbr0", route)
        self.assertIn("try-restart libvirtd", fake.text())  # new hook → reload
        self.assertTrue(fake.scripts[-1][0].endswith(edge.APPLY_SCRIPT))

    def test_unchanged_host_only_reapplies(self):
        conf = _conf()
        bid = paths.box_id(conf)
        vm_ip = net.env_ip(bid, "prod")
        texts = {
            edge.ROUTE_FILE: edge.render_route(f"{bid}/prod", net.domain_name(bid, "prod"),
                                               vm_ip, "virbr0"),
            edge.RULESET_FILE: edge.render_ruleset(vm_ip),
            edge.APPLY_SCRIPT: edge.render_apply_script(),
            edge.HOOK_FILE: edge.render_hook(),
        }
        shas = {p: edge._sha(t) for p, t in texts.items()}
        fake = FakeHost(route=texts[edge.ROUTE_FILE], shas=shas)
        self.assertEqual(_reconcile(conf, fake), "unchanged")
        self.assertFalse([s for s, i in fake.scripts if i is not None])
        self.assertNotIn("try-restart", fake.text())
        self.assertTrue(fake.scripts[-1][0].endswith(edge.APPLY_SCRIPT))

    def test_foreign_owner_is_a_hard_error(self):
        fake = FakeHost(route="OWNER=other-123abc/prod\nIP=192.168.122.99\n")
        with self.assertRaises(errors.EnvError) as cm:
            _reconcile(_conf(), fake)
        self.assertIn("other-123abc/prod", str(cm.exception))
        self.assertEqual(fake.scripts, [])  # only the read

    def test_no_public_hosts_tears_down_our_route(self):
        conf = _conf(host=False)
        fake = FakeHost(route=f"OWNER={paths.box_id(conf)}/prod\n")
        self.assertEqual(_reconcile(conf, fake), "removed")
        self.assertIn(f"{edge.APPLY_SCRIPT} down", fake.text())

    def test_no_public_hosts_and_no_route_is_a_noop(self):
        fake = FakeHost()
        self.assertIsNone(_reconcile(_conf(host=False), fake))
        self.assertEqual(fake.scripts, [])


if __name__ == "__main__":
    unittest.main()
