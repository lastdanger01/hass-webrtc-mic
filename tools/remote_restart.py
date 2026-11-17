#!/usr/bin/env python3
"""Restart the remote Home Assistant through the supervisor API.

The supervisor is reached over the Home Assistant WebSocket API, so no
port has to be opened. The command only restarts Home Assistant, it does
not touch the supervisor itself or any setting.

Usage:
    python3 remote_restart.py
"""

import base64
import json
import os
import socket
import ssl
import sys
import time

HA_HOST = "ndm9514770.duckdns.org"
HA_PORT = 1234
TOKEN_FILE = "/home/lastdanger/.openclaw/secrets/ha_token_remote"

_SUP_ID = 0


def _token():
    with open(TOKEN_FILE) as handle:
        return handle.read().strip()


def api_socket():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    s = ctx.wrap_socket(
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
            if opcode == 0x9:
                mask = os.urandom(4)
                s.send(bytes([0x8A, 0x80 | ln]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
                continue
            if opcode in (0x8, 0xA) or opcode != 0x1:
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
        raise RuntimeError(f"{endpoint} failed: {result.get('error')}")
    return result.get("result")


if __name__ == "__main__":
    endpoint = sys.argv[1] if len(sys.argv) > 1 else "/core/restart"
    s, send, recv = api_socket()
    try:
        supervisor(send, recv, "post", endpoint, {})
        print(f"OK   {endpoint} accepted")
    finally:
        try:
            s.close()
        except Exception:
            pass
