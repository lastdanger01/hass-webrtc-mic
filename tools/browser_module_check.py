#!/usr/bin/env python3
"""Load the webrtc-mic card module in a real browser and report the error.

The page probe showed that ``customElements.get('webrtc-mic-camera')`` is
false even though the module was fetched. This script imports the module
directly in the page and reports the real exception.

Usage:
    python3 browser_module_check.py <base-url>
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

CHROME = shutil.which("google-chrome") or shutil.which("chromium")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class CDP:
    def __init__(self, ws_url):
        hostport = ws_url.split("//")[1].split("/")[0]
        path = "/" + "/".join(ws_url.split("//")[1].split("/")[1:])
        self.sock = socket.create_connection(tuple(hostport.split(":")), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.send(
            (
                f"GET {path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self._id = 0
        self.events = []

    def _raw(self, method, params=None):
        self._id += 1
        data = json.dumps(
            {"id": self._id, "method": method, "params": params or {}}
        ).encode()
        mask = os.urandom(4)
        n = len(data)
        head = (
            bytes([0x81, 0x80 | n])
            if n < 126
            else bytes([0x81, 0x80 | 126]) + n.to_bytes(2, "big")
        )
        self.sock.send(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, method, params=None):
        self._id += 1
        mid = self._id
        data = json.dumps({"id": mid, "method": method, "params": params or {}}).encode()
        mask = os.urandom(4)
        n = len(data)
        head = (
            bytes([0x81, 0x80 | n])
            if n < 126
            else bytes([0x81, 0x80 | 126]) + n.to_bytes(2, "big")
        )
        self.sock.send(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        while True:
            msg = self._recv()
            if "method" in msg and "id" not in msg:
                self.events.append(msg)
                handler = getattr(self, "_on", None)
                if handler is not None:
                    handler(msg)
                continue

            def _noop(self):
                return None
            if msg.get("id") == mid:
                return msg.get("result", {})

    def _recv(self):
        def read(n):
            d = b""
            while len(d) < n:
                c = self.sock.recv(n - len(d))
                if not c:
                    raise RuntimeError("cdp closed")
                d += c
            return d

        h = read(2)
        op = h[0] & 0x0F
        ln = h[1] & 0x7F
        if ln == 126:
            ln = int.from_bytes(read(2), "big")
        elif ln == 127:
            ln = int.from_bytes(read(8), "big")
        data = read(ln)
        if op == 0x9:
            mask = os.urandom(4)
            self.sock.send(
                bytes([0x8A, 0x80 | ln])
                + mask
                + bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            )
            return self._recv()
        return json.loads(data.decode())


def main():
    base = sys.argv[1].rstrip("/")
    cert_mode = "ignore"
    args = sys.argv[2:]
    for i, a in enumerate(args):
        if a == "--cert-mode":
            cert_mode = args[i + 1]
    port = free_port()
    profile = tempfile.mkdtemp(prefix="chrome-mod-")
    cmd = [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "about:blank",
    ]
    if cert_mode == "ignore":
        cmd.insert(3, "--ignore-certificate-errors")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        target = None
        for _ in range(60):
            try:
                data = json.load(
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2)
                )
                pages = [t for t in data if t.get("type") == "page"]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                time.sleep(0.5)
        if not target:
            print("no devtools")
            return 1

        cdp = CDP(target["webSocketDebuggerUrl"])
        cdp.send("Runtime.enable")
        cdp.send("Log.enable")
        cdp.send("Page.enable")
        cdp.send("Security.enable")

        cert_errors = []

        def _cert(params):
            cert_errors.append(params.get("errorType"))
            event_id = params.get("eventId")
            if event_id is None:
                return
            cdp._raw(
                "Security.handleCertificateError",
                {"eventId": event_id, "action": "continue"},
            )

        cdp._on = _cert
        if cert_mode == "manual":
            cdp.send("Security.setOverrideCertificateErrors", {"override": True})

        cdp.send("Page.navigate", {"url": base + "/"})
        time.sleep(8)
        print(f"=== 憑證錯誤 (cert-mode={cert_mode}) ===")
        print("  " + (", ".join(cert_errors) if cert_errors else "(無)"))

        expr = """
        (async () => {
          const out = {steps: []};
          const url = %s + '/webrtc_mic/webrtc-mic-camera.js?v=51fdcdb51922';
          try {
            const mod = await import(url);
            out.steps.push('module import OK');
            out.exports = Object.keys(mod);
          } catch (e) {
            out.steps.push('module import FAILED: ' + e.name + ': ' + e.message);
          }
          out.cardDefined = !!customElements.get('webrtc-mic-camera');
          out.videoRTCDefined = !!customElements.get('video-rtc');
          try {
            const v = await import(%s + '/webrtc_mic/video-rtc.js?v=c43ebec95e6b');
            out.steps.push('video-rtc import OK, exports: ' + Object.keys(v).join(','));
          } catch (e) {
            out.steps.push('video-rtc import FAILED: ' + e.name + ': ' + e.message);
          }
          try {
            const p = await import(%s + '/webrtc_mic/digital-ptz.js?v=0a80c41c58b1');
            out.steps.push('digital-ptz import OK, exports: ' + Object.keys(p).join(','));
          } catch (e) {
            out.steps.push('digital-ptz import FAILED: ' + e.name + ': ' + e.message);
          }
          out.cardDefinedAfter = !!customElements.get('webrtc-mic-camera');
          out.videoRTCDefinedAfter = !!customElements.get('video-rtc');
          return JSON.stringify(out, null, 2);
        })()
        """ % (json.dumps(base), json.dumps(base), json.dumps(base))

        res = cdp.send(
            "Runtime.evaluate",
            {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        print("=== MODULE LOAD ===")
        print(res.get("result", {}).get("value"))
        if res.get("exceptionDetails"):
            print("=== EXCEPTION ===")
            print(json.dumps(res["exceptionDetails"], indent=2)[:1500])

        print("=== CONSOLE / ERRORS ===")
        for msg in cdp.events:
            if msg["method"] == "Log.entryAdded":
                e = msg["params"]["entry"]
                print(f"  [{e.get('level')}] {e.get('text')}")
            elif msg["method"] == "Runtime.exceptionThrown":
                d = msg["params"].get("exceptionDetails", {})
                print(f"  [exception] {d.get('text')}")
                print(
                    "   ",
                    str(d.get("exception", {}).get("description", ""))[:600],
                )
            elif msg["method"] == "Runtime.consoleAPICalled":
                p = msg["params"]
                args = p.get("args", [])
                text = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
                print(f"  [{p.get('type')}] {text}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
