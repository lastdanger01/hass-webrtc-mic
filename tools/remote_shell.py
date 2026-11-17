#!/usr/bin/env python3
"""Run a command on the remote Home Assistant and read the output back.

The remote HA (ndm9514770.duckdns.org:1234) runs the "Advanced SSH & Web
Terminal" add-on. Its terminal is a ttyd instance that Home Assistant
exposes through the supervisor ingress. The terminal itself is a full shell,
but its output is mangled by tmux and the terminal escape sequences.

So the command is run in a way that its output can be read back cleanly:
the output is written to a file below /config/www, which Home Assistant
serves at /local/. The file is removed afterwards.

Usage:
    python3 remote_shell.py "ls -la /config/custom_components"
    python3 remote_shell.py --keep "some command"   # keep the output file
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
OUT_NAME = "rs_out.txt"

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
    """Open the Home Assistant WebSocket API and authenticate."""
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
        if n < 126:
            head = bytes([0x81, 0x80 | n])
        else:
            head = bytes([0x81, 0x80 | 126]) + n.to_bytes(2, "big")
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
    """Call a supervisor API endpoint over the HA WebSocket API."""
    global _SUP_ID
    _SUP_ID += 1
    msg = {
        "id": _SUP_ID,
        "type": "supervisor/api",
        "method": method,
        "endpoint": endpoint,
    }
    if data is not None:
        msg["data"] = data
    send(msg)
    result = recv()
    if not result.get("success"):
        raise RuntimeError(f"supervisor {endpoint} failed: {result.get('error')}")
    return result.get("result")


def open_terminal():
    """Open the ttyd websocket of the SSH add-on."""
    s, send, recv = api_socket()
    try:
        sess = supervisor(send, recv, "post", "/ingress/session", {})["session"]
        info = supervisor(send, recv, "get", f"/addons/{SSH_ADDON}/info")
        token = info["ingress_url"].strip("/").split("/")[-1]
    finally:
        s.close()

    w = _ssl().wrap_socket(
        socket.create_connection((HA_HOST, HA_PORT), timeout=60), server_hostname=HA_HOST
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
    """Read a file from the remote /config/www over HTTPS."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = f"https://{HA_HOST}:{HA_PORT}/local/{name}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_token()}"})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return resp.read()


def run(command, timeout=90, keep=False):
    """Run a command in the remote terminal and return its output."""
    # A unique name per run, so a leftover file from an earlier run can
    # never be mistaken for the result.
    stamp = f"{int(time.time() * 1000)}"
    out_name = f"rs_{stamp}.txt"
    done_name = f"rs_{stamp}.done"

    w, wsend = open_terminal()
    try:
        wsend(json.dumps({"AuthToken": "", "columns": 200, "rows": 50}).encode())
        time.sleep(2.0)

        # Reset the line. The terminal is a shared tmux session, a
        # half typed command from an earlier run would otherwise swallow
        # the next one.
        wsend(b"\x03")
        time.sleep(0.4)
        wsend(b"\x03")
        time.sleep(0.4)

        # Remove the leftovers of earlier runs. This happens before the
        # command writes its own output, so the cleanup cannot delete
        # the file that is being read back.
        wsend(
            b"0find /config/www -maxdepth 1 -name 'rs_*' ! -name '"
            + out_name.encode()
            + b"' ! -name '"
            + done_name.encode()
            + b"' -delete 2>/dev/null; true\n"
        )
        time.sleep(0.8)

        script = (
            f"mkdir -p /config/www; "
            f"( {command} ) > /config/www/{out_name} 2>&1; "
            f"echo DONE > /config/www/{done_name}\n"
        )
        wsend(("0" + script).encode())
        time.sleep(1.5)

        # Wait for the .done marker.
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if b"DONE" in _fetch_local(done_name):
                    break
            except Exception:
                pass
            time.sleep(1.5)

        try:
            data = _fetch_local(out_name)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"could not read the output: {exc}") from exc
    finally:
        w.close()

    return data.decode(errors="replace")


if __name__ == "__main__":
    keep = "--keep" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--keep"]
    cmd = " ".join(args) if args else "pwd; hostname; id"
    print(run(cmd, keep=keep))
