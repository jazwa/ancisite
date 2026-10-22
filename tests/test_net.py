import json
import unittest

from anci.render.cloudinit import render_network_config
from anci.net import (
    domain_name, env_ip, stable_mac, sanitize_domain,
    DEFAULT_GATEWAY, STATIC_BASE, STATIC_POOL,
)

BID = "ancisite-3f9a2c"


class TestStaticAddressing(unittest.TestCase):
    def test_deterministic_and_in_pool(self):
        for name in ("my-vm", "staging-vm", "a", "b"):
            ip = env_ip(BID, name)
            last_octet = int(ip.rsplit(".", 1)[1])
            self.assertTrue(ip.startswith("192.168.122."))
            self.assertTrue(STATIC_BASE <= last_octet < STATIC_BASE + STATIC_POOL)
            self.assertEqual(env_ip(BID, name), ip)  # deterministic

    def test_distinct_names_usually_differ(self):
        ips = {env_ip(BID, n) for n in [f"vm-{i}" for i in range(32)]}
        self.assertGreater(len(ips), 8)  # hash spread over the 64-address pool

    def test_projects_get_different_seeds(self):
        # different box ids (projects) move the address even for the same env
        self.assertNotEqual(env_ip("proj-a-000000", "dev"), env_ip("proj-b-111111", "dev"))

    def test_mac_deterministic_not_ip_derived_path(self):
        # mac and ip hash from different salts: a MAC change never moves an IP
        self.assertEqual(stable_mac(BID, "my-vm"), stable_mac(BID, "my-vm"))
        self.assertTrue(stable_mac(BID, "my-vm").startswith("52:54:00:"))

    def test_domain_name_namespaces_by_project(self):
        self.assertEqual(domain_name(BID, "dev"), f"anci-{BID}-dev")

    def test_sanitizers(self):
        self.assertEqual(sanitize_domain("my vm/2"), "my-vm-2")


class TestRenderNetworkConfig(unittest.TestCase):
    def test_shape(self):
        out = render_network_config("192.168.122.77/24", "52:54:00:15:66:5f", DEFAULT_GATEWAY)
        nic = json.loads(out)["network"]["ethernets"]["nic"]   # JSON is valid YAML
        self.assertEqual(json.loads(out)["network"]["version"], 2)
        self.assertEqual(nic["match"], {"macaddress": "52:54:00:15:66:5f"})
        self.assertIs(nic["dhcp4"], False)
        self.assertEqual(nic["addresses"], ["192.168.122.77/24"])
        self.assertEqual(nic["routes"], [{"to": "default", "via": DEFAULT_GATEWAY}])
        self.assertEqual(nic["nameservers"], {"addresses": [DEFAULT_GATEWAY]})


if __name__ == "__main__":
    unittest.main()
