#!/usr/bin/env python3
"""Voice translator web server (stdlib only).

    python3 server.py [--backend agy|muse|ollama|echo] [--port 8787] [--lan] [--public] [--key K]

--lan binds every interface and serves HTTPS with a self-signed certificate so a phone on the
same Wi-Fi can use the microphone (browsers only allow it on https:// or localhost).
--public additionally opens a Cloudflare quick tunnel (`cloudflared`) and prints a public
https://*.trycloudflare.com URL. Because that URL is reachable by anyone, --public always
protects /api/* with an access key (random unless --key is given); the printed URL carries it.

GET  /               -> index.html
GET  /api/health     -> {"backend": ..., "ready": bool, "detail": str}
GET  /api/tts?text=&lang= -> audio/mpeg (Microsoft Edge neural voices via `uvx edge-tts`; the browser
                        falls back to speechSynthesis when this fails)
POST /api/translate  -> NDJSON stream: ({"status": str} | {"delta": str})* then
                        {"done": true, "text": str, "ms": int} or {"error": str}
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import tempfile
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import translator

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
CERT_DIR = os.path.join(HERE, "certs")
MAX_TEXT_CHARS = 2000
MAX_CONCURRENT = 2

# Server-side text-to-speech. Phones and macOS ship no Tagalog voice, so the browser cannot read
# translations aloud by itself; Edge's free neural voices cover fil-PH. Run through uvx so the
# server itself stays dependency-free (first call downloads the edge-tts package).
TTS_BIN = ["uvx", "edge-tts"]
TTS_TIMEOUT_SECONDS = 30
TTS_VOICES = {
    "tl": "fil-PH-BlessicaNeural", "ko": "ko-KR-SunHiNeural", "en": "en-US-JennyNeural",
    "ja": "ja-JP-NanamiNeural", "zh": "zh-CN-XiaoxiaoNeural", "vi": "vi-VN-HoaiMyNeural",
    "id": "id-ID-GadisNeural", "th": "th-TH-PremwadeeNeural", "ne": "ne-NP-HemkalaNeural",
    "bn": "bn-BD-NabanitaNeural", "ur": "ur-PK-UzmaNeural", "km": "km-KH-SreymomNeural",
}
TTS_CACHE: "collections.OrderedDict[str, bytes]" = collections.OrderedDict()
TTS_CACHE_MAX = 200
TTS_LOCK = threading.Lock()

# Optional shared secret for /api/*. None means open (fine for localhost / home Wi-Fi).
ACCESS_KEY = None
TUNNEL_BIN = "cloudflared"
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com\b")


def generate_key() -> str:
    return secrets.token_urlsafe(18)


def is_authorized(headers, path: str) -> bool:
    """/api/* needs the key in X-Access-Key or ?key= when ACCESS_KEY is set."""
    if not ACCESS_KEY:
        return True
    given = headers.get("X-Access-Key") if hasattr(headers, "get") else None
    if not given:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        given = (qs.get("key") or [None])[0]
    return bool(given) and secrets.compare_digest(given, ACCESS_KEY)


def tunnel_command(port: int, tls: bool) -> list[str]:
    origin = f"{'https' if tls else 'http'}://127.0.0.1:{port}"
    cmd = [TUNNEL_BIN, "tunnel", "--no-autoupdate", "--url", origin]
    if tls:
        cmd.append("--no-tls-verify")  # our own self-signed cert
    return cmd


def parse_tunnel_url(line: str):
    m = TUNNEL_URL_RE.search(line)
    return m.group(0) if m and not m.group(0).startswith("https://api.") else None


def start_tunnel(port: int, tls: bool, timeout: float = 40.0):
    """Start cloudflared and return (process, public_url). Raises RuntimeError on failure."""
    proc = subprocess.Popen(tunnel_command(port, tls), stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    found: list[str] = []
    deadline = time.monotonic() + timeout

    def pump():
        assert proc.stderr is not None
        for line in proc.stderr:
            url = parse_tunnel_url(line)
            if url and not found:
                found.append(url)
            if "ERR" in line and "failed" in line.lower():
                sys.stderr.write("cloudflared: " + line)

    threading.Thread(target=pump, daemon=True).start()
    while not found and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    if not found:
        proc.kill()
        raise RuntimeError("cloudflared did not report a public URL (is the network up?)")
    return proc, found[0]


def voice_for(lang: str):
    return TTS_VOICES.get(lang)


def tts_command(text: str, voice: str, out_path: str) -> list[str]:
    return TTS_BIN + ["--voice", voice, "--text", text, "--write-media", out_path]


def synthesize(text: str, lang: str) -> bytes:
    voice = voice_for(lang)
    if not voice:
        raise ValueError(f"no voice for language {lang!r}")
    key = hashlib.sha1(f"{voice}\x00{text}".encode("utf-8")).hexdigest()
    with TTS_LOCK:
        if key in TTS_CACHE:
            TTS_CACHE.move_to_end(key)
            return TTS_CACHE[key]
    with tempfile.TemporaryDirectory(prefix="vt-tts-") as d:
        out = os.path.join(d, "out.mp3")
        subprocess.run(tts_command(text, voice, out), check=True, capture_output=True,
                       timeout=TTS_TIMEOUT_SECONDS)
        with open(out, "rb") as f:
            data = f.read()
    if not data:
        raise RuntimeError("tts produced no audio")
    with TTS_LOCK:
        TTS_CACHE[key] = data
        while len(TTS_CACHE) > TTS_CACHE_MAX:
            TTS_CACHE.popitem(last=False)
    return data


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
        if path.startswith("/api/") and not is_authorized(self.headers, self.path):
            return self._json(401, {"error": "access key required"})
        if path in ("/", "/index.html"):
            with open(INDEX, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/tts":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            text = (qs.get("text") or [""])[0].strip()
            lang = (qs.get("lang") or [""])[0]
            if not text or len(text) > MAX_TEXT_CHARS:
                return self._json(400, {"error": "text is required (max %d chars)" % MAX_TEXT_CHARS})
            if not voice_for(lang):
                return self._json(404, {"error": f"no voice for {lang!r}"})
            try:
                data = synthesize(text, lang)
            except (subprocess.SubprocessError, OSError, RuntimeError) as e:
                self.log_message("tts failed: %s", e)
                return self._json(503, {"error": f"tts failed: {type(e).__name__}"})
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/health":
            ready, detail = self.backend.ready()
            self._json(200, {"backend": self.backend.name, "ready": ready, "detail": detail,
                             "languages": translator.LANGUAGES, "tts": sorted(TTS_VOICES)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/translate":
            return self._json(404, {"error": "not found"})
        if not is_authorized(self.headers, self.path):
            return self._json(401, {"error": "access key required"})
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
    ap.add_argument("--backend", default="agy", choices=["agy", "agy-oneshot", "muse", "ollama", "echo"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--lan", action="store_true",
                    help="bind all interfaces and serve HTTPS (self-signed) for phones on the same Wi-Fi")
    ap.add_argument("--public", action="store_true",
                    help="open a Cloudflare quick tunnel and print a public https URL (needs `brew install cloudflared`)")
    ap.add_argument("--key", default=os.environ.get("VT_ACCESS_KEY"),
                    help="access key required for /api/* (auto-generated with --public)")
    args = ap.parse_args(argv)

    global ACCESS_KEY
    ACCESS_KEY = args.key or (generate_key() if args.public else None)

    Handler.backend = translator.get_backend(args.backend)
    ready, detail = Handler.backend.ready()
    if ready and hasattr(Handler.backend, "warm"):
        # Start the interpreter session now so the first sentence does not pay the CLI start-up.
        def _warm():
            try:
                Handler.backend.warm()
                print("interpreter session ready", flush=True)
            except Exception as e:  # noqa: BLE001 - report, keep serving
                print(f"WARNING: interpreter session did not start: {e}", flush=True)
        threading.Thread(target=_warm, daemon=True).start()
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
    tunnel = None
    if args.public:
        try:
            tunnel, url = start_tunnel(args.port, tls=args.lan)
        except (OSError, RuntimeError) as e:
            print(f"ERROR: could not open tunnel: {e}", flush=True)
            httpd.server_close()
            return 1
        print(f"  anywhere (share this): {url}/?key={ACCESS_KEY}", flush=True)
        print("  The key is required; the page remembers it after the first visit.", flush=True)
    elif ACCESS_KEY:
        print(f"  access key required for /api/*: open the page with ?key={ACCESS_KEY}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if tunnel is not None:
            tunnel.terminate()
        if hasattr(Handler.backend, "close"):
            Handler.backend.close()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
