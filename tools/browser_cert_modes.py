#!/usr/bin/env python3
"""Compare how the card module loads with and without a valid certificate.

The expired certificate does not stop the page itself from loading.
What matters is whether the JavaScript module that defines the card
element is allowed to run. This script fetches and imports the module
in the page and reports what happens in each certificate mode.

Usage:
    python3 browser_cert_modes.py <base-url>
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
MODULE = "/webrtc_mic/webrtc-mic-camera.js?v=51fdcdb51922"


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
        self._on = None

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
                if self._on:
                    self._on(msg)
                continue
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


PROBE = """
(async () => {
  const out = {};
  out.isSecureContext = window.isSecureContext;
  out.origin = window.location.origin;
  out.mediaDevices = !!navigator.mediaDevices;
  const url = %s + %s;
  try {
    const r = await fetch(url, {cache: 'no-store'});
    out.fetch = 'ok status=' + r.status + ' type=' + r.headers.get('content-type');
    const text = await r.text();
    out.fetchedBytes = text.length;
    out.fetchedHasDefine = /customElements\\.define/.test(text);
  } catch (e) {
    out.fetch = 'FAILED: ' + e.name + ': ' + e.message;
  }
  try {
    await import(url);
    out.import = 'ok';
  } catch (e) {
    out.import = 'FAILED: ' + e.name + ': ' + e.message;
  }
  out.cardDefined = !!customElements.get('webrtc-mic-camera');
  return JSON.stringify(out, null, 2);
})()
"""


def run(base, mode):
    port = free_port()
    profile = tempfile.mkdtemp(prefix=f"chrome-{mode}-")
    cmd = [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "about:blank",
    ]
    if mode == "ignore":
        cmd.insert(3, "--ignore-certificate-errors")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            return {"error": "no devtools"}

        cdp = CDP(target["webSocketDebuggerUrl"])
        cdp.send("Runtime.enable")
        cdp.send("Log.enable")
        cdp.send("Page.enable")
        cdp.send("Security.enable")

        cert_errors = []

        def _cert(msg):
            params = msg.get("params", {})
            cert_errors.append(params.get("errorType"))
            eid = params.get("eventId")
            if eid is not None:
                cdp._raw(
                    "Security.handleCertificateError",
                    {"eventId": eid, "action": "continue"},
                )

        cdp._on = _cert
        if mode == "manual":
            cdp.send("Security.setOverrideCertificateErrors", {"override": True})

        cdp.send("Page.navigate", {"url": base + "/"})
        time.sleep(8)

        res = cdp.send(
            "Runtime.evaluate",
            {
                "expression": PROBE % (json.dumps(base), json.dumps(MODULE)),
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        out = json.loads(res.get("result", {}).get("value") or "{}")
        out["certErrors"] = cert_errors
        out["consoleErrors"] = [
            msg["params"]["entry"].get("text")
            for msg in cdp.events
            if msg["method"] == "Log.entryAdded"
            and msg["params"]["entry"].get("level") == "error"
        ]
        return out
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    base = sys.argv[1].rstrip("/")
    for mode in ("manual", "ignore"):
        print(f"########## cert-mode = {mode} ##########")
        print(json.dumps(run(base, mode), indent=2, ensure_ascii=False))
        print()
