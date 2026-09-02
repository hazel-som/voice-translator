#!/usr/bin/env python3
"""Voice translator web server (stdlib only).

    python3 server.py [--backend agy|muse|ollama|echo] [--port 8787] [--lan]

--lan binds every interface and serves HTTPS with a self-signed certificate so a phone on the
same Wi-Fi can use the microphone (browsers only allow it on https:// or localhost).

GET  /               -> index.html
GET  /api/health     -> {"backend": ..., "ready": bool, "detail": str}
POST /api/translate  -> NDJSON stream: ({"status": str} | {"delta": str})* then
                        {"done": true, "text": str, "ms": int} or {"error": str}
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import translator

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
CERT_DIR = os.path.join(HERE, "certs")
MAX_TEXT_CHARS = 2000
MAX_CONCURRENT = 2


class Handler(BaseHTTPRequestHandler):
    backend = None  # set by main()
    gate = threading.BoundedSemaphore(MAX_CONCURRENT)

    def log_message(self, fmt, *args):  # quieter than the default
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # -- helpers ---------------------------------------------------------
    def _json(self, status: int, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_line(self, obj) -> None:
        self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            with open(INDEX, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/health":
            ready, detail = self.backend.ready()
            self._json(200, {"backend": self.backend.name, "ready": ready, "detail": detail,
                             "languages": translator.LANGUAGES})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/translate":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return self._json(400, {"error": "invalid JSON body"})
        text = (body.get("text") or "").strip()
        source = body.get("source") or "ko"
        target = body.get("target") or "tl"
        if not text:
            return self._json(400, {"error": "text is required"})
        if len(text) > MAX_TEXT_CHARS:
            return self._json(413, {"error": f"text longer than {MAX_TEXT_CHARS} chars"})
        if source not in translator.LANGUAGES or target not in translator.LANGUAGES:
            return self._json(400, {"error": "unsupported language"})
        if source == target:
            return self._json(400, {"error": "source and target are the same"})

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        started = time.monotonic()
        try:
            with self.gate:
                for kind, payload in self.backend.translate(text, source, target):
                    if kind == "delta":
                        self._write_line({"delta": payload})
                    elif kind == "status":
                        self._write_line({"status": payload})
                    elif kind == "done":
                        ms = int((time.monotonic() - started) * 1000)
                        self._write_line({"done": True, "text": payload, "ms": ms})
                        self.log_message("translated %s->%s in %dms (%d chars)", source, target, ms, len(text))
                    else:
                        self._write_line({"error": payload})
                        self.log_message("translate error: %s", payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away; nothing to do
        except Exception as e:  # keep the server alive whatever the backend does
            try:
                self._write_line({"error": f"{type(e).__name__}: {e}"})
            except OSError:
                pass


def lan_addresses() -> list[str]:
    """IPv4 addresses other devices on the local network can reach this machine at."""
    found: list[str] = []
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "inet" and not parts[1].startswith("127."):
                found.append(parts[1])
    except (OSError, subprocess.SubprocessError):
        pass
    if not found:  # portable fallback: the interface used to reach the outside world
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
                sk.connect(("10.255.255.255", 1))
                found.append(sk.getsockname()[0])
        except OSError:
            pass
    return found


def ensure_self_signed_cert(cert_dir: str, hosts: list[str]) -> tuple[str, str]:
    """Return (cert_path, key_path), generating a self-signed cert whose SAN covers `hosts`."""
    os.makedirs(cert_dir, exist_ok=True)
    cert, key, stamp = (os.path.join(cert_dir, n) for n in ("cert.pem", "key.pem", "hosts.txt"))
    want = ",".join(sorted(set(hosts)))
    if os.path.exists(cert) and os.path.exists(key) and os.path.exists(stamp):
        with open(stamp) as f:
            if f.read().strip() == want:
                return cert, key
    san = ["DNS:localhost", "IP:127.0.0.1"] + [f"IP:{h}" for h in sorted(set(hosts))]
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "3650", "-nodes",
         "-subj", "/CN=voice-translator", "-addext", "subjectAltName=" + ",".join(san),
         "-keyout", key, "-out", cert],
        check=True, capture_output=True,
    )
    os.chmod(key, 0o600)
    with open(stamp, "w") as f:
        f.write(want)
    return cert, key


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="voice translator web server")
    ap.add_argument("--backend", default="agy", choices=["agy", "muse", "ollama", "echo"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--lan", action="store_true",
                    help="bind all interfaces and serve HTTPS (self-signed) for phones on the same Wi-Fi")
    args = ap.parse_args(argv)

    Handler.backend = translator.get_backend(args.backend)
    ready, detail = Handler.backend.ready()
    host = "0.0.0.0" if args.lan else args.host
    httpd = ThreadingHTTPServer((host, args.port), Handler)
    httpd.daemon_threads = True
    if args.lan:
        lan = lan_addresses()
        cert, key = ensure_self_signed_cert(CERT_DIR, lan)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        print("voice translator (HTTPS, self-signed)", flush=True)
        for ip in lan:
            print(f"  phone / other devices:  https://{ip}:{args.port}/", flush=True)
        print(f"  this machine:           https://127.0.0.1:{args.port}/", flush=True)
        print("  The browser will warn about the certificate once; choose to proceed anyway.", flush=True)
    else:
        print(f"voice translator  http://{host}:{args.port}/", flush=True)
    print(f"backend: {Handler.backend.name}  ready: {ready}  ({detail})", flush=True)
    if not ready:
        print("NOTE: backend not ready; requests will return an error until it is.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
