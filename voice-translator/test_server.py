import os
import subprocess
import tempfile
import unittest

import server


class SelfSignedCertTest(unittest.TestCase):
    def test_creates_cert_and_key_with_lan_ip_in_san(self):
        with tempfile.TemporaryDirectory() as d:
            cert, key = server.ensure_self_signed_cert(d, ["192.168.0.228"])
            self.assertTrue(os.path.exists(cert))
            self.assertTrue(os.path.exists(key))
            text = subprocess.run(["openssl", "x509", "-in", cert, "-noout", "-text"],
                                  capture_output=True, text=True, check=True).stdout
            self.assertIn("IP Address:192.168.0.228", text)
            self.assertIn("DNS:localhost", text)
            self.assertIn("IP Address:127.0.0.1", text)

    def test_reuses_existing_cert(self):
        with tempfile.TemporaryDirectory() as d:
            cert, _ = server.ensure_self_signed_cert(d, ["10.0.0.5"])
            first = os.stat(cert).st_mtime_ns
            cert2, _ = server.ensure_self_signed_cert(d, ["10.0.0.5"])
            self.assertEqual(cert, cert2)
            self.assertEqual(first, os.stat(cert2).st_mtime_ns)

    def test_regenerates_when_lan_ip_changes(self):
        with tempfile.TemporaryDirectory() as d:
            server.ensure_self_signed_cert(d, ["10.0.0.5"])
            cert, _ = server.ensure_self_signed_cert(d, ["10.0.0.9"])
            text = subprocess.run(["openssl", "x509", "-in", cert, "-noout", "-text"],
                                  capture_output=True, text=True, check=True).stdout
            self.assertIn("IP Address:10.0.0.9", text)


class LanAddressesTest(unittest.TestCase):
    def test_returns_private_ipv4_strings(self):
        addrs = server.lan_addresses()
        self.assertIsInstance(addrs, list)
        for a in addrs:
            self.assertRegex(a, r"^\d+\.\d+\.\d+\.\d+$")
            self.assertNotEqual(a, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
