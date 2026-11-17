#!/usr/bin/env python3
"""Check the real browser state of the remote Home Assistant.

Headless Chrome driven over CDP. Two certificate modes matter:

``--cert-mode=manual``
    Simulates what a human does: the browser shows the certificate
    warning and the user clicks "Advanced -> Proceed". This is the
    case that matters, because Chrome only treats an origin as a
    secure context when the certificate is valid.

``--cert-mode=ignore``
    The certificate error is ignored at the network layer.

Usage:
    python3 browser_check.py <url> [--cert-mode manual|ignore] [--token FILE]
                             [--lovelace PATH]
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
    """A very small CDP client over a raw websocket."""

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
        self.handlers = []

    def on(self, method):
        def deco(fn):
            self.handlers.append((method, fn))
            return fn

        return deco

    def _raw_send(self, method, params=None):
        """Send a command without waiting for its response."""
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
                for name, fn in self.handlers:
                    if msg["method"] == name:
                        fn(msg["params"])
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


PROBE = r"""
(() => {
  const out = {
    isSecureContext: window.isSecureContext,
    origin: window.location.origin,
    protocol: window.location.protocol,
    hasMediaDevices: !!navigator.mediaDevices,
    hasGetUserMedia: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
    cardElementDefined: !!customElements.get('webrtc-mic-camera'),
    videoRTCDefined: !!customElements.get('video-rtc'),
    cardInDom: document.querySelectorAll('webrtc-mic-camera').length,
    scripts: Array.from(document.querySelectorAll('script'))
      .map(s => s.src || (s.textContent || '').slice(0, 160))
      .filter(x => x && x.toLowerCase().includes('webrtc')),
    resources: (window.__hass && window.__hass.states) ? 'logged-in' : 'not-logged-in',
  };
  return JSON.stringify(out, null, 2);
})()
"""


def main():
    url = sys.argv[1]
    cert_mode = "manual"
    token_file = None
    lovelace = "/lovelace/0"
    args = sys.argv[2:]
    for i, a in enumerate(args):
        if a == "--cert-mode":
            cert_mode = args[i + 1]
        elif a == "--token":
            token_file = args[i + 1]
        elif a == "--lovelace":
            lovelace = args[i + 1]

    port = free_port()
    profile = tempfile.mkdtemp(prefix="chrome-check-")
    cmd = [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--autoplay-policy=no-user-gesture-required",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "about:blank",
    ]
    if cert_mode == "ignore":
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
            print("could not reach chrome devtools")
            return 1

        cdp = CDP(target["webSocketDebuggerUrl"])
        cdp.send("Runtime.enable")
        cdp.send("Log.enable")
        cdp.send("Page.enable")
        cdp.send("Network.enable")
        cdp.send("Security.enable")

        cert_errors = []
        requests = []

        @cdp.on("Security.certificateError")
        def _cert(params):
            cert_errors.append(params.get("errorType"))
            # Fire and forget: sending and waiting for a response from
            # inside the read loop would deadlock the client.
            cdp._raw_send(
                "Security.handleCertificateError",
                {
                    "eventId": params["eventId"],
                    "action": "continue",
                },
            )

        @cdp.on("Network.requestWillBeSent")
        def _req(params):
            u = params.get("request", {}).get("url", "")
            if "webrtc" in u.lower():
                requests.append(u)

        cdp.send("Security.setIgnoreCertificateErrors", {"ignore": cert_mode == "ignore"})
        cdp.send(
            "Security.setOverrideCertificateErrors",
            {"override": cert_mode == "manual"},
        )

        print(f"=== NAVIGATE (cert-mode={cert_mode}) ===")
        nav = cdp.send("Page.navigate", {"url": url})
        print(json.dumps(nav, indent=2)[:600])
        time.sleep(10)

        print("=== 憑證錯誤 ===")
        print("  " + (", ".join(cert_errors) if cert_errors else "(無)"))
        if cert_errors:
            print("  -> 使用者必須手動按「繼續前往」才能進來")

        print("=== PAGE PROBE ===")
        res = cdp.send(
            "Runtime.evaluate",
            {"expression": PROBE, "returnByValue": True, "awaitPromise": True},
        )
        print(res.get("result", {}).get("value"))

        if token_file:
            token = open(token_file).read().strip()
            seed = json.dumps(
                {
                    "hassTokens": {
                        "access_token": token,
                        "refresh_token": token,
                        "expires": 9999999999,
                        "expires_in": 9999999999,
                        "clientId": "http://ndm9514770.duckdns.org:1234/",
                        "refresh_token_expires": 9999999999,
                        "expires_in_timestamp": 9999999999,
                    }
                }
            )
            cdp.send(
                "Runtime.evaluate",
                {
                    "expression": (
                        "localStorage.setItem('hassTokens', "
                        + json.dumps(seed)
                        + "); location.href = "
                        + json.dumps(lovelace)
                    )
                },
            )
            time.sleep(20)

            print("=== LOVELACE PROBE ===")
            res = cdp.send(
                "Runtime.evaluate",
                {"expression": PROBE, "returnByValue": True, "awaitPromise": True},
            )
            print(res.get("result", {}).get("value"))

        print("=== 網路請求 (webrtc) ===")
        for u in dict.fromkeys(requests):
            print("  " + u)

        print("=== CONSOLE ===")
        for msg in cdp.events:
            if msg["method"] == "Runtime.consoleAPICalled":
                args = msg["params"].get("args", [])
                text = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
                print(f"  [{msg['params'].get('type')}] {text}")
            elif msg["method"] == "Log.entryAdded":
                e = msg["params"]["entry"]
                print(f"  [{e.get('level')}] {e.get('text')}")
            elif msg["method"] == "Runtime.exceptionThrown":
                d = msg["params"].get("exceptionDetails", {})
                print(f"  [exception] {d.get('text')} {d.get('exception', {}).get('description', '')}"[:400])
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
