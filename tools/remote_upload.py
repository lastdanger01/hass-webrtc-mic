#!/usr/bin/env python3
"""Upload files to the remote Home Assistant through the SSH add-on terminal.

The remote HA runs the "Advanced SSH & Web Terminal" add-on. Its terminal
is a ttyd instance that Home Assistant exposes through the supervisor
ingress. There is no SSH or SMB from the outside, so the files are pushed
through the terminal itself: every file is base64 encoded and written in
chunks with "printf", then decoded on the other side.

Usage:
    python3 remote_upload.py <local-file> <remote-path>
    python3 remote_upload.py --checksum <remote-path>
"""

import base64
import json
import os
import socket
import ssl
import sys
import time
import urllib.request

HA_HOST = "ndm9514770.duckdns.org"
HA_PORT = 1234
TOKEN_FILE = "/home/lastdanger/.openclaw/secrets/ha_token_remote"
SSH_ADDON = "a0d7b954_ssh"
CHUNK = 2400

_SUP_ID = 0


def _ssl():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _token():
    with open(TOKEN_FILE) as handle:
        return handle.read().strip()


def api_socket():
    s = _ssl().wrap_socket(
        socket.create_connection((HA_HOST, HA_PORT), timeout=30), server_hostname=HA_HOST
    )
    key = base64.b64encode(os.urandom(16)).decode()
    s.send(
        (
            f"GET /api/websocket HTTP/1.1\r\nHost: {HA_HOST}:{HA_PORT}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {_token()}\r\n\r\n"
        ).encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)

    def send(obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x81, 0x80 | n]) if n < 126 else bytes([0x81, 0x80 | 126]) + n.to_bytes(2, "big")
        s.send(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv():
        """Read one text frame, skipping ping/pong/binary frames."""
        while True:
            head = s.recv(2)
            if not head:
                raise RuntimeError("websocket closed")
            opcode = head[0] & 0x0F
            ln = head[1] & 0x7F
            if ln == 126:
                ln = int.from_bytes(s.recv(2), "big")
            elif ln == 127:
                ln = int.from_bytes(s.recv(8), "big")
            data = b""
            while len(data) < ln:
                chunk = s.recv(ln - len(data))
                if not chunk:
                    raise RuntimeError("websocket closed")
                data += chunk
            if opcode == 0x9:  # ping -> pong
                mask = os.urandom(4)
                s.send(bytes([0x8A, 0x80 | ln]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
                continue
            if opcode in (0x8, 0xA):  # close / pong
                continue
            if opcode != 0x1:
                continue
            return json.loads(data.decode())

    recv()
    send({"type": "auth", "access_token": _token()})
    recv()
    return s, send, recv


def supervisor(send, recv, method, endpoint, data=None):
    global _SUP_ID
    _SUP_ID += 1
    msg = {"id": _SUP_ID, "type": "supervisor/api", "method": method, "endpoint": endpoint}
    if data is not None:
        msg["data"] = data
    send(msg)
    result = recv()
    if not result.get("success"):
        raise RuntimeError(f"supervisor {endpoint} failed: {result.get('error')}")
    return result.get("result")


def open_terminal():
    s, send, recv = api_socket()
    try:
        sess = supervisor(send, recv, "post", "/ingress/session", {})["session"]
        info = supervisor(send, recv, "get", f"/addons/{SSH_ADDON}/info")
        token = info["ingress_url"].strip("/").split("/")[-1]
    finally:
        s.close()

    w = _ssl().wrap_socket(
        socket.create_connection((HA_HOST, HA_PORT), timeout=120), server_hostname=HA_HOST
    )
    key = base64.b64encode(os.urandom(16)).decode()
    w.send(
        (
            f"GET /api/hassio_ingress/{token}/ws HTTP/1.1\r\nHost: {HA_HOST}:{HA_PORT}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Protocol: tty\r\n"
            f"Cookie: ingress_session={sess}\r\n\r\n"
        ).encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += w.recv(4096)
    status = buf.split(b"\r\n")[0].decode(errors="replace")
    if "101" not in status:
        raise RuntimeError(f"ingress websocket failed: {status}")

    def wsend(raw):
        mask = os.urandom(4)
        n = len(raw)
        if n < 126:
            head = bytes([0x82, 0x80 | n])
        elif n < 65536:
            head = bytes([0x82, 0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head = bytes([0x82, 0x80 | 127]) + n.to_bytes(8, "big")
        w.send(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(raw)))

    return w, wsend


def _fetch_local(name):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = f"https://{HA_HOST}:{HA_PORT}/local/{name}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_token()}"})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return resp.read()


def upload(local_path, remote_path):
    """Upload a local file to the remote host."""
    with open(local_path, "rb") as handle:
        raw = handle.read()
    payload = base64.b64encode(raw).decode()

    # A unique name per upload, so a leftover file from an earlier run
    # can never be mistaken for the result.
    stamp = f"{int(time.time() * 1000)}"
    b64_remote = f"/tmp/upload_{stamp}.b64"
    sha_name = f"up_{stamp}.sha"
    done_name = f"up_{stamp}.done"

    w, wsend = open_terminal()
    try:
        wsend(json.dumps({"AuthToken": "", "columns": 200, "rows": 50}).encode())
        time.sleep(2.0)

        # Reset the line. The terminal is a shared tmux session, a half
        # typed command from an earlier run would otherwise swallow the
        # next one.
        wsend(b"\x03")
        time.sleep(0.4)
        wsend(b"\x03")
        time.sleep(0.4)

        wsend(f"0rm -f {b64_remote} /config/www/{sha_name} /config/www/{done_name}\n".encode())
        time.sleep(1.0)

        chunks = [payload[i : i + CHUNK] for i in range(0, len(payload), CHUNK)]
        for index, chunk in enumerate(chunks, start=1):
            wsend(f"0printf '%s' '{chunk}' >> {b64_remote}\n".encode())
            if index % 5 == 0:
                time.sleep(0.5)
            else:
                time.sleep(0.15)

        time.sleep(1.5)
        wsend(
            f"0base64 -d {b64_remote} > '{remote_path}' 2>/config/www/{sha_name}.err; "
            f"sha256sum '{remote_path}' > /config/www/{sha_name} 2>&1; "
            f"echo DONE > /config/www/{done_name}\n".encode()
        )

        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                if b"DONE" in _fetch_local(done_name):
                    break
            except Exception:
                pass
            time.sleep(1.5)

        time.sleep(0.5)

        try:
            got = _fetch_local(sha_name).decode(errors="replace").split()[0]
        except Exception:
            got = "?"
    finally:
        w.close()

    import hashlib

    want = hashlib.sha256(raw).hexdigest()
    ok = got == want
    print(f"{'OK  ' if ok else '[X] '} {remote_path}  {got}")
    if not ok:
        raise SystemExit(f"checksum mismatch: expected {want}")
    return ok


if __name__ == "__main__":
    if sys.argv[1] == "--checksum":
        print(_fetch_local(sys.argv[2]).decode(errors="replace"))
    else:
        upload(sys.argv[1], sys.argv[2])
