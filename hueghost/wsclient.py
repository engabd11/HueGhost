"""Tiny RFC 6455 WebSocket client (text frames, ping/pong, close). stdlib only.

Enough for Hue Sync's Public Control endpoint: unfragmented JSON text frames
in both directions, plus keep-alive pings.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    pass


class WebSocketClosed(WebSocketError):
    pass


class WebSocket:
    def __init__(self, host: str, port: int, path: str = "/", timeout: float = 5.0):
        self.host, self.port, self.path = host, int(port), path
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.server_header = ""
        self._buf = b""

    # -- connection --------------------------------------------------------
    def connect(self) -> "WebSocket":
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
               % (self.path, self.host, self.port, key))
        s.sendall(req.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            if not chunk:
                s.close()
                raise WebSocketError("connection closed during handshake")
            head += chunk
            if len(head) > 65536:
                s.close()
                raise WebSocketError("handshake response too large")
        head, _, rest = head.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        if " 101 " not in lines[0]:
            s.close()
            raise WebSocketError("handshake failed: " + lines[0])
        hdrs = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            hdrs[k.strip().lower()] = v.strip()
        expect = base64.b64encode(hashlib.sha1(key.encode() + _GUID).digest()).decode()
        if hdrs.get("sec-websocket-accept") != expect:
            s.close()
            raise WebSocketError("bad Sec-WebSocket-Accept")
        self.server_header = hdrs.get("server", "")
        self.sock = s
        self._buf = rest
        return self

    def settimeout(self, t: float | None) -> None:
        if self.sock:
            self.sock.settimeout(t)

    def close(self, code: int = 1000) -> None:
        if not self.sock:
            return
        try:
            self._send_frame(OP_CLOSE, struct.pack(">H", code))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None

    # -- frames ------------------------------------------------------------
    def _send_frame(self, op: int, payload: bytes) -> None:
        if not self.sock:
            raise WebSocketClosed("not connected")
        head = bytes([0x80 | op])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def ping(self, data: bytes = b"") -> None:
        self._send_frame(OP_PING, data)

    def _fill(self, n: int) -> None:
        while len(self._buf) < n:
            if not self.sock:
                raise WebSocketClosed("not connected")
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketClosed("peer closed")
            self._buf += chunk

    def recv(self) -> tuple[int, bytes]:
        """Next frame as (opcode, payload). Pings are answered here; raises
        socket.timeout when idle beyond the timeout and WebSocketClosed on close."""
        while True:
            self._fill(2)
            b0, b1 = self._buf[0], self._buf[1]
            op = b0 & 0x0F
            masked = b1 & 0x80
            n = b1 & 0x7F
            hdr = 2
            if n == 126:
                self._fill(4)
                n = struct.unpack(">H", self._buf[2:4])[0]
                hdr = 4
            elif n == 127:
                self._fill(10)
                n = struct.unpack(">Q", self._buf[2:10])[0]
                hdr = 10
            if masked:
                self._fill(hdr + 4)
                mask = self._buf[hdr:hdr + 4]
                hdr += 4
            else:
                mask = None
            self._fill(hdr + n)
            payload = self._buf[hdr:hdr + n]
            self._buf = self._buf[hdr + n:]
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if op == OP_PING:
                try:
                    self._send_frame(OP_PONG, payload)
                except OSError:
                    pass
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                self.close()
                raise WebSocketClosed("close frame")
            return op, payload

    def recv_text(self) -> str:
        while True:
            op, payload = self.recv()
            if op == OP_TEXT:
                return payload.decode("utf-8", "replace")
