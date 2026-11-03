import json
import unittest

from anci.render.cloudinit import render_network_config
from anci.net import (
    domain_name, first_free, pool, stable_mac, sanitize_domain,
    DEFAULT_GATEWAY, STATIC_BASE, STATIC_POOL,
)

BID = "ancisite-3f9a2c"


class TestStaticAddressing(unittest.TestCase):
    def test_pool_is_the_static_range(self):
        ips = pool()
        self.assertEqual(len(ips), STATIC_POOL)
        self.assertEqual(ips[0], f"192.168.122.{STATIC_BASE}")
        self.assertEqual(ips[-1], f"192.168.122.{STATIC_BASE + STATIC_POOL - 1}")

    def test_first_free_skips_taken_addresses(self):
        self.assertEqual(first_free([]), "192.168.122.50")
        self.assertEqual(first_free(["192.168.122.50", "192.168.122.51"]), "192.168.122.52")
        # a gap left by a removed box is reused before the end of the pool
        self.assertEqual(first_free(["192.168.122.50", "192.168.122.52"]), "192.168.122.51")
        self.assertIsNone(first_free(pool()))

    def test_mac_deterministic(self):
        self.assertEqual(stable_mac(BID, "my-vm"), stable_mac(BID, "my-vm"))
        self.assertNotEqual(stable_mac(BID, "a"), stable_mac(BID, "b"))
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
