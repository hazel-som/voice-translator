import os
import subprocess
import tempfile
import unittest
import unittest.mock

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


class TtsTest(unittest.TestCase):
    def test_voice_for_known_and_unknown_language(self):
        self.assertEqual(server.voice_for("tl"), "fil-PH-BlessicaNeural")
        self.assertEqual(server.voice_for("ko"), "ko-KR-SunHiNeural")
        self.assertIsNone(server.voice_for("xx"))

    def test_tts_command_passes_voice_text_and_output(self):
        cmd = server.tts_command("Kumusta", "fil-PH-BlessicaNeural", "/tmp/o.mp3")
        self.assertEqual(cmd[cmd.index("--voice") + 1], "fil-PH-BlessicaNeural")
        self.assertEqual(cmd[cmd.index("--text") + 1], "Kumusta")
        self.assertEqual(cmd[cmd.index("--write-media") + 1], "/tmp/o.mp3")

    def test_synthesize_returns_audio_bytes_and_caches(self):
        with tempfile.TemporaryDirectory() as d:
            stub = os.path.join(d, "fake-tts")
            with open(stub, "w") as f:
                f.write('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = "--write-media" ] && out="$2"; shift; done\n'
                        'printf "MP3DATA-$$" > "$out"\n')
            os.chmod(stub, 0o755)
            with unittest.mock.patch.object(server, "TTS_BIN", [stub]):
                server.TTS_CACHE.clear()
                a = server.synthesize("Kumusta", "tl")
                b = server.synthesize("Kumusta", "tl")
            self.assertTrue(a.startswith(b"MP3DATA-"))
            self.assertEqual(a, b)  # second call served from cache (same pid suffix)

    def test_synthesize_unknown_language_raises(self):
        with self.assertRaises(ValueError):
            server.synthesize("hi", "xx")
