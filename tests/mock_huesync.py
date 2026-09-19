"""A stand-in for the Hue Sync app's Public Control WebSocket (stdlib only)."""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class MockHueSync:
    def __init__(self, state="bridge_connected", mode="video", intensity="high", bri=56):
        self.state, self.mode, self.intensity, self.bri = state, mode, intensity, bri
        self.received: list[dict] = []
        self.raw_received: list[str] = []
        self.connections = 0
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        self.accepting = True
        threading.Thread(target=self._accept_loop, daemon=True).start()

    # -- lifecycle -----------------------------------------------------------
    def close(self):
        self._stop = True
        self.drop_clients()
        try:
            self._srv.close()
        except OSError:
            pass

    def drop_clients(self):
        with self._lock:
            for c in self._clients:
                try:
                    # shutdown() sends FIN even while another thread is blocked in
                    # recv(); a bare close() would not on Linux (fd still referenced)
                    c.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    c.close()
                except OSError:
                    pass
            self._clients.clear()

    # -- helpers ---------------------------------------------------------------
    def state_event(self) -> str:
        return json.dumps({"event": "app_state_update", "data": {
            "state": self.state, "mode": self.mode, "intensity": self.intensity, "bri": self.bri}})

    def broadcast_state(self):
        with self._lock:
            for c in list(self._clients):
                try:
                    _send_text(c, self.state_event())
                except OSError:
                    pass

    def wait_for(self, name: str, timeout: float = 3.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in self.received:
                if m.get("command") == name:
                    return m
            time.sleep(0.02)
        return None

    def commands(self) -> list[str]:
        return [m.get("command") for m in self.received]

    # -- server ------------------------------------------------------------------
    def _accept_loop(self):
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            if not self.accepting:
                c.close()
                continue
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c: socket.socket):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = c.recv(4096)
                if not chunk:
                    return
                head += chunk
            key = None
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"sec-websocket-key:"):
                    key = line.split(b":", 1)[1].strip()
            accept = base64.b64encode(hashlib.sha1(key + GUID).digest()).decode()
            c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       "Sec-WebSocket-Accept: %s\r\nServer: Hue Sync Public Control\r\n\r\n" % accept).encode())
            with self._lock:
                self._clients.append(c)
                self.connections += 1
            _send_text(c, self.state_event())
            buf = b""
            while not self._stop:
                chunk = c.recv(65536)
                if not chunk:
                    return
                buf += chunk
                while True:
                    frame, buf = _parse_frame(buf)
                    if frame is None:
                        break
                    op, payload = frame
                    if op == 0x8:
                        return
                    if op == 0x9:
                        _send_frame(c, 0xA, payload)
                        continue
                    if op == 0x1:
                        self._on_text(c, payload.decode("utf-8", "replace"))
        except OSError:
            pass
        finally:
            with self._lock:
                if c in self._clients:
                    self._clients.remove(c)
            try:
                c.close()
            except OSError:
                pass

    def _on_text(self, c, text):
        self.raw_received.append(text)
        try:
            msg = json.loads(text)
        except ValueError:
            return
        self.received.append(msg)
        cmd = msg.get("command")
        data = msg.get("data") or {}
        if cmd == "start_sync":
            if self.state == "bridge_connected":
                self.state = "syncing"
        elif cmd == "stop_sync":
            if self.state == "syncing":
                self.state = "bridge_connected"
        elif cmd == "set_app_mode":
            if self.state != "syncing":
                return           # real app: only acts on a live session (no reply)
            self.mode = data.get("mode", self.mode)
        elif cmd == "set_intensity":
            if self.state != "syncing":
                return
            self.intensity = data.get("intensity", self.intensity)
        elif cmd == "inc_bri":
            self.bri = max(0, min(100, self.bri + int(data.get("step", 0))))
        else:
            return   # unknown command: real app logs and ignores
        _send_text(c, self.state_event())


def _send_frame(c: socket.socket, op: int, payload: bytes):
    head = bytes([0x80 | op])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    c.sendall(head + payload)


def _send_text(c: socket.socket, text: str):
    _send_frame(c, 0x1, text.encode("utf-8"))


def _parse_frame(buf: bytes):
    if len(buf) < 2:
        return None, buf
    b0, b1 = buf[0], buf[1]
    op = b0 & 0x0F
    masked = b1 & 0x80
    n = b1 & 0x7F
    i = 2
    if n == 126:
        if len(buf) < 4:
            return None, buf
        n = struct.unpack(">H", buf[2:4])[0]
        i = 4
    elif n == 127:
        if len(buf) < 10:
            return None, buf
        n = struct.unpack(">Q", buf[2:10])[0]
        i = 10
    mask = None
    if masked:
        if len(buf) < i + 4:
            return None, buf
        mask = buf[i:i + 4]
        i += 4
    if len(buf) < i + n:
        return None, buf
    payload = buf[i:i + n]
    if mask:
        payload = bytes(b ^ mask[k % 4] for k, b in enumerate(payload))
    return (op, payload), buf[i + n:]
