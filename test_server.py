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


class AccessKeyTest(unittest.TestCase):
    def test_no_key_configured_allows_everything(self):
        with unittest.mock.patch.object(server, "ACCESS_KEY", None):
            self.assertTrue(server.is_authorized({}, "/api/translate"))

    def test_key_in_header_or_query(self):
        with unittest.mock.patch.object(server, "ACCESS_KEY", "s3cret"):
            self.assertTrue(server.is_authorized({"X-Access-Key": "s3cret"}, "/api/translate"))
            self.assertTrue(server.is_authorized({}, "/api/tts?key=s3cret&text=hi"))
            self.assertFalse(server.is_authorized({"X-Access-Key": "wrong"}, "/api/translate"))
            self.assertFalse(server.is_authorized({}, "/api/translate"))

    def test_generated_key_is_long_and_url_safe(self):
        k = server.generate_key()
        self.assertGreaterEqual(len(k), 16)
        self.assertRegex(k, r"^[A-Za-z0-9_-]+$")


class TunnelTest(unittest.TestCase):
    def test_tunnel_command_plain_and_tls_origin(self):
        cmd = server.tunnel_command(8787, tls=False)
        self.assertEqual(cmd[0], "cloudflared")
        self.assertIn("http://127.0.0.1:8787", cmd)
        cmd = server.tunnel_command(8787, tls=True)
        self.assertIn("https://127.0.0.1:8787", cmd)
        self.assertIn("--no-tls-verify", cmd)

    def test_parse_tunnel_url(self):
        line = "2026-09-02T08:00:00Z INF |  https://quiet-owl-1234.trycloudflare.com                                  |"
        self.assertEqual(server.parse_tunnel_url(line), "https://quiet-owl-1234.trycloudflare.com")
        self.assertIsNone(server.parse_tunnel_url("INF Registered tunnel connection connIndex=0"))
        self.assertIsNone(server.parse_tunnel_url("https://api.trycloudflare.com/tunnel"))  # not a hostname URL


class FakeBackend:
    name = "fake"

    def ready(self):
        return True, "fake"

    def translate(self, text, source, target):
        yield "status", "working"
        yield "delta", "Kumusta"
        yield "done", "Kumusta"


class FailingBackend(FakeBackend):
    def translate(self, text, source, target):
        yield "error", "boom"


class ConversationLoggingTest(unittest.TestCase):
    """POST /api/translate stores the finished sentence under the session the browser sent."""

    def setUp(self):
        import http.client
        import json
        import threading
        from http.server import ThreadingHTTPServer
        import storage
        self.http, self.json = http.client, json
        self.dir = tempfile.TemporaryDirectory()
        self.store = storage.ConversationStore(os.path.join(self.dir.name, "conv.db"))
        self.patches = [unittest.mock.patch.object(server.Handler, "backend", FakeBackend()),
                        unittest.mock.patch.object(server.Handler, "store", self.store),
                        unittest.mock.patch.object(server, "ACCESS_KEY", None)]
        for p in self.patches:
            p.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for p in self.patches:
            p.stop()
        self.store.close()
        self.dir.cleanup()

    def _post(self, body):
        conn = self.http.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/translate", body=self.json.dumps(body),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read().decode()
        conn.close()
        return resp.status, data

    def _turns(self):
        import sqlite3
        with sqlite3.connect(self.store.path) as db:
            return db.execute("SELECT session_id, source, target, source_text, translated_text FROM turns").fetchall()

    def test_done_translation_is_saved_with_session(self):
        status, body = self._post({"text": "안녕", "source": "ko", "target": "tl", "session": "page-load-0001"})
        self.assertEqual(status, 200)
        self.assertIn('"done": true', body)
        self.assertEqual(self._turns(), [("page-load-0001", "ko", "tl", "안녕", "Kumusta")])

    def test_missing_or_invalid_session_falls_back_to_unknown(self):
        self._post({"text": "안녕", "source": "ko", "target": "tl"})
        self._post({"text": "안녕", "source": "ko", "target": "tl", "session": "bad id!"})
        self.assertEqual([t[0] for t in self._turns()], ["unknown", "unknown"])

    def test_failed_translation_is_not_saved(self):
        with unittest.mock.patch.object(server.Handler, "backend", FailingBackend()):
            _, body = self._post({"text": "안녕", "source": "ko", "target": "tl", "session": "page-load-0001"})
        self.assertIn('"error"', body)
        self.assertEqual(self._turns(), [])

    def test_storage_failure_does_not_break_the_response(self):
        with unittest.mock.patch.object(self.store, "save_turn", side_effect=RuntimeError("disk full")):
            status, body = self._post({"text": "안녕", "source": "ko", "target": "tl", "session": "page-load-0001"})
        self.assertEqual(status, 200)
        self.assertIn('"done": true', body)

    def test_no_store_configured_still_translates(self):
        with unittest.mock.patch.object(server.Handler, "store", None):
            status, body = self._post({"text": "안녕", "source": "ko", "target": "tl", "session": "page-load-0001"})
        self.assertEqual(status, 200)
        self.assertIn('"done": true', body)
