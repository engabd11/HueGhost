"""The ghost: a muted mpv playing the same stream, driven over its JSON IPC.

Unlike v1, the ghost's position is *read back* from mpv (``observe_property
time-pos``) instead of being estimated from ``--start`` and the wall clock, so
seek latency, keyframe snapping, buffering stalls and startup delay are all
measured rather than assumed.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from .lockstep import Action, Pause, Resume, Seek, Speed
from .winutil import mpv_audio_device

log = logging.getLogger("hue-ghost.ghost")
HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_CONF = os.path.join(HERE, "ghost_input.conf")

OBSERVED = {
    1: "time-pos",
    2: "pause",
    3: "speed",
    4: "paused-for-cache",
    5: "eof-reached",
    6: "core-idle",
}


class MpvIpc:
    """One duplex JSON-IPC connection to mpv (Windows named pipe / unix socket)."""

    def __init__(self, path: str):
        self.path = path
        self._fh = None
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._req_id = 0
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self.on_event: Callable[[dict], None] | None = None
        self._reader: threading.Thread | None = None
        self.closed = False

    def connect(self, alive: Callable[[], bool], timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            if not alive():
                raise RuntimeError("mpv exited before its IPC endpoint opened")
            try:
                if sys.platform == "win32":
                    self._fh = open(self.path, "r+b", buffering=0)
                else:
                    self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self._sock.connect(self.path)
                break
            except OSError as e:
                last_err = e
                time.sleep(0.2)
        else:
            raise RuntimeError("could not open mpv IPC %s: %s" % (self.path, last_err))
        self._reader = threading.Thread(target=self._read_loop, name="mpv-ipc-reader", daemon=True)
        self._reader.start()

    # -- raw io -------------------------------------------------------------
    def _read_chunk(self) -> bytes:
        if self._sock is not None:
            return self._sock.recv(65536)
        # Windows: a blocking ReadFile on a synchronous pipe handle also blocks
        # WriteFile from other threads on that handle, so poll PeekNamedPipe and
        # only ever read what is already there.
        import ctypes
        import msvcrt
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        handle = msvcrt.get_osfhandle(self._fh.fileno())  # type: ignore[union-attr]
        avail = wintypes.DWORD(0)
        while not self.closed:
            ok = k32.PeekNamedPipe(wintypes.HANDLE(handle), None, 0, None, ctypes.byref(avail), None)
            if not ok:
                return b""                      # pipe gone
            if avail.value:
                return self._fh.read(avail.value)  # type: ignore[union-attr]
            time.sleep(0.01)
        return b""

    def _write(self, data: bytes) -> None:
        with self._lock:
            if self._sock is not None:
                self._sock.sendall(data)
            else:
                self._fh.write(data)  # type: ignore[union-attr]

    def _read_loop(self) -> None:
        buf = b""
        while not self.closed:
            try:
                chunk = self._read_chunk()
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                self._dispatch(msg)
        self.closed = True

    def _dispatch(self, msg: dict) -> None:
        rid = msg.get("request_id")
        if rid is not None and rid in self._pending:
            ev, box = self._pending.pop(rid)
            box.append(msg)
            ev.set()
            return
        if "event" in msg and self.on_event:
            try:
                self.on_event(msg)
            except Exception:
                log.exception("ipc event handler failed")

    # -- commands ------------------------------------------------------------
    def send(self, *cmd: Any) -> None:
        """Fire-and-forget command."""
        self._write((json.dumps({"command": list(cmd)}) + "\n").encode("utf-8"))

    def request(self, *cmd: Any, timeout: float = 2.0) -> dict | None:
        with self._lock:
            self._req_id += 1
            rid = self._req_id
        ev, box = threading.Event(), []
        self._pending[rid] = (ev, box)
        self._write((json.dumps({"command": list(cmd), "request_id": rid}) + "\n").encode("utf-8"))
        if ev.wait(timeout):
            return box[0] if box else None
        self._pending.pop(rid, None)
        return None

    def close(self) -> None:
        self.closed = True
        try:
            if self._sock is not None:
                self._sock.close()
            elif self._fh is not None:
                self._fh.close()
        except OSError:
            pass


def resolve_screen(screen_name: str, screen_index) -> int | None:
    """Turn the stable display name into today's enumeration index.

    mpv's --fs-screen-name does not match Windows device names, but its
    --fs-screen=N index follows EnumDisplayMonitors order - the same order
    winutil.list_displays() reports - so the name is resolved at launch time and
    survives displays being added/removed.
    """
    if screen_name and sys.platform == "win32":
        from .winutil import list_displays
        for d in list_displays():
            if d.name.lower() == screen_name.lower():
                return d.index
        log.warning("display %s not present; falling back to %s", screen_name,
                    "index %s" % screen_index if screen_index not in (None, "") else "current display")
    if screen_index is not None and str(screen_index) != "":
        return int(screen_index)
    return None


def mpv_args(cfg, title: str, audio_only: bool = False, audio_device: str = "") -> list[str]:
    """The ghost's command line.

    A video ghost is muted and on screen - Hue Sync reacts to its picture. A
    music ghost is the other way round: no window at all, and the sound is the
    whole point, so it is played (at full volume) into a device you cannot
    hear, which is the device Hue Sync's music mode listens to."""
    g = cfg.section("ghost")
    args = [g.get("mpv_path") or "mpv", "--no-config", "--no-border",
            "--osd-level=0", "--osc=no",
            "--sub-visibility=no", "--sub-auto=no", "--audio-file-auto=no",
            "--hwdec=" + str(g.get("hwdec") or "auto"),
            "--keep-open=no", "--hr-seek=yes", "--focus-on=never",
            "--cursor-autohide=always", "--msg-level=all=warn",
            "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
            "--demuxer-readahead-secs=5",
            "--input-default-bindings=no",
            "--input-conf=" + INPUT_CONF]
    if audio_only:
        vol = max(0, min(100, int(g.get("music_volume", 100) or 100)))
        args += ["--mute=no", "--volume=%d" % vol, "--vid=no", "--force-window=no",
                 "--audio-client-name=hue-ghost"]
        dev = mpv_audio_device(audio_device)
        if dev:
            args += ["--audio-device=" + dev]
        args += list(g.get("extra_args") or [])
        args += ["--input-ipc-server=" + cfg.get("ghost.ipc"), "--title=" + title]
        return args
    args += ["--mute=yes", "--volume=0"]
    idx = resolve_screen(g.get("screen_name") or "", g.get("screen_index"))
    if idx is not None:
        args += ["--screen=%d" % idx, "--fs-screen=%d" % idx]
    elif g.get("screen_name") and sys.platform != "win32":
        args += ["--screen-name=" + g["screen_name"], "--fs-screen-name=" + g["screen_name"]]
    if g.get("fullscreen", True):
        args += ["--fullscreen=yes"]
    else:
        args += ["--fullscreen=no", "--geometry=" + str(g.get("geometry") or "28%x28%-40-40")]
    args += list(g.get("extra_args") or [])
    args += ["--input-ipc-server=" + cfg.get("ghost.ipc"), "--title=" + title]
    return args


class GhostPlayer:
    """mpv process + live state mirrored from IPC events."""

    def __init__(self, proc: subprocess.Popen, ipc: MpvIpc, item_id: str, errf=None):
        self.proc = proc
        self.ipc = ipc
        self.item_id = item_id
        self.errf = errf
        self.started_mono = time.monotonic()
        # mirrored state
        self._time_pos: float | None = None
        self._time_pos_mono = 0.0
        self.paused = False
        self.speed = 1.0
        self.buffering = False
        self.eof = False
        self.core_idle = True
        self.user_quit = False
        self.end_reason: str | None = None
        self.last_seek_mono = float("-inf")
        self.seeks = 0
        self.nudges = 0
        ipc.on_event = self._on_event

    # -- lifecycle -----------------------------------------------------------
    @classmethod
    def launch(cls, cfg, url: str, http_header: str, start_pos: float, item_id: str,
               err_path: str | None = None, audio_only: bool = False,
               audio_device: str = "") -> "GhostPlayer":
        args = mpv_args(cfg, "hue-ghost", audio_only=audio_only, audio_device=audio_device)
        args += ["--http-header-fields=" + http_header, "--start=%.3f" % max(0.0, start_pos), url]
        errf = open(err_path, "wb") if err_path else subprocess.DEVNULL
        creation = 0
        if sys.platform == "win32":
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=errf,
                                stdin=subprocess.DEVNULL, creationflags=creation)
        ipc = MpvIpc(cfg.get("ghost.ipc"))
        g = cls(proc, ipc, item_id, errf if err_path else None)
        try:
            ipc.connect(alive=g.alive)
        except Exception:
            g.kill()
            raise
        for pid, name in OBSERVED.items():
            ipc.send("observe_property", pid, name)
        return g

    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        try:
            if self.alive() and not self.ipc.closed:
                self.ipc.send("quit")
                for _ in range(10):
                    if not self.alive():
                        break
                    time.sleep(0.1)
        except Exception:
            pass
        self.ipc.close()
        try:
            if self.alive():
                self.proc.terminate()
                self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        if self.errf is not None:
            try:
                self.errf.close()
            except OSError:
                pass
            self.errf = None

    # -- state ---------------------------------------------------------------
    def _on_event(self, msg: dict) -> None:
        ev = msg.get("event")
        if ev == "property-change":
            name, data = msg.get("name"), msg.get("data")
            if name == "time-pos":
                if isinstance(data, (int, float)):
                    self._time_pos = float(data)
                    self._time_pos_mono = time.monotonic()
            elif name == "pause":
                self.paused = bool(data)
            elif name == "speed" and isinstance(data, (int, float)):
                self.speed = float(data)
            elif name == "paused-for-cache":
                self.buffering = bool(data)
            elif name == "eof-reached":
                self.eof = bool(data)
            elif name == "core-idle":
                self.core_idle = bool(data)
        elif ev == "end-file":
            self.end_reason = msg.get("reason")
            if self.end_reason == "quit":
                self.user_quit = True
        elif ev == "shutdown":
            self.user_quit = self.user_quit or self.end_reason == "quit"

    @property
    def has_position(self) -> bool:
        return self._time_pos is not None

    @property
    def stalled(self) -> bool:
        """Playing, but no time-pos update for > 1.5 s."""
        return (self.has_position and not self.paused and not self.buffering
                and time.monotonic() - self._time_pos_mono > 1.5)

    @property
    def pos(self) -> float | None:
        if self._time_pos is None:
            return None
        if self.paused or self.buffering or self.core_idle:
            return self._time_pos
        dt = min(time.monotonic() - self._time_pos_mono, 1.0)
        return self._time_pos + dt * self.speed

    # -- control -------------------------------------------------------------
    def apply(self, actions: list[Action], now_mono: float) -> None:
        for a in actions:
            if isinstance(a, Seek):
                self.ipc.send("seek", round(max(0.0, a.pos), 3), "absolute", "exact")
                self.last_seek_mono = now_mono
                self.seeks += 1
                # until mpv reports the new position, assume we landed
                self._time_pos = max(0.0, a.pos)
                self._time_pos_mono = now_mono
            elif isinstance(a, Speed):
                self.ipc.send("set_property", "speed", round(a.value, 4))
                if abs(a.value - 1.0) > 1e-6:
                    self.nudges += 1
                self.speed = a.value
            elif isinstance(a, Pause):
                self.ipc.send("set_property", "pause", True)
                self.paused = True
            elif isinstance(a, Resume):
                self.ipc.send("set_property", "pause", False)
                self.paused = False

    def toggle_fullscreen(self) -> None:
        self.ipc.send("cycle", "fullscreen")
