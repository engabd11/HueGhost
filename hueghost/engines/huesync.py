"""Drive the official Hue Sync desktop app over its "Public Control" WebSocket.

Protocol (Hue Sync 1.13, recovered from the app; enable *Settings > Allow
public control* in the app):

  ws://127.0.0.1:24851/           (any path; header "Server: Hue Sync Public Control")
  <- {"event":"app_state_update","data":{"state":"bridge_connected","mode":"video","intensity":"high","bri":56}}
  -> {"command":"start_sync"}
  -> {"command":"stop_sync"}
  -> {"command":"set_app_mode","data":{"mode":"video"}}          video | games | music | scenes
  -> {"command":"set_intensity","data":{"intensity":"high"}}     subtle | moderate | high | extreme
  -> {"command":"inc_bri","data":{"step":10}}                    signed step
  states: bridge_disconnected | bridge_connected | syncing

Live-verified quirks (1.13.1): set_app_mode / set_intensity only act on an
ACTIVE sync session and are silently ignored otherwise, so the reconcile
order is start_sync -> set_app_mode -> set_intensity. Every accepted command
is answered with an app_state_update; ignored ones get nothing.

The engine is *declarative*: the daemon states what it wants (sync on/off,
mode, intensity, entertainment area) and a background thread keeps reconciling
the app's reported state towards it - so a Hue Sync restart mid-movie simply
re-asserts sync.

Entertainment area: the API cannot select one, but the app reads its selection
(``SelectedGroup`` in ``%APPDATA%\HueSync\bridge.json``) at start-up. Switching
therefore means: stop sync -> kill the app -> patch the file -> relaunch it
``-silent`` (~2-3 s) -> sync. Only done when the wanted area differs from the
current one, i.e. when playback moves to a device bound to another room.

The engine only ever stops a sync it started itself, so a sync you start by
hand in the app (a game, say) is left alone.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time

from ..winutil import (hue_sync_exe, hue_sync_groups, hue_sync_kill, hue_sync_launch,
                       hue_sync_selected_area, hue_sync_write_selected_area)
from ..wsclient import WebSocket, WebSocketClosed, WebSocketError
from . import Engine, EngineState

log = logging.getLogger("hue-ghost.huesync")

STATE_DISCONNECTED = "bridge_disconnected"
STATE_CONNECTED = "bridge_connected"
STATE_SYNCING = "syncing"
INTENSITIES = ("subtle", "moderate", "high", "extreme")
MODES = ("video", "games", "music", "scenes")

RESEND_AFTER_S = 2.5        # wait this long for an app_state_update before re-sending a command
BACKOFF_MIN_S, BACKOFF_MAX_S = 1.0, 30.0
PING_EVERY_S = 20.0


def build_command(name: str, **data) -> str:
    msg: dict = {"command": name}
    if data:
        msg["data"] = data
    return json.dumps(msg)


class HueSyncEngine(Engine):
    name = "huesync"

    def __init__(self, host: str = "127.0.0.1", port: int = 24851, mode: str = "video",
                 intensity: str = "", launch_exe: str = "", connect_timeout: float = 3.0):
        self.host, self.port = host, int(port)
        self.want_mode = (mode or "").lower() or None
        self.want_intensity = (intensity or "").lower() or None
        self.launch_exe = launch_exe or ""
        self.connect_timeout = connect_timeout

        self._st = EngineState(name=self.name)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._ws: WebSocket | None = None
        self._last_sent: dict[str, float] = {}
        # monotonic() counts from boot: "never" must be -inf, not 0, or the
        # cooldowns below silently hold for the first minute after start-up
        self._last_launch = float("-inf")
        self._warned_absent = float("-inf")
        self._started_by_us = False       # we sent the start_sync that is running
        self.want_area: str | None = None
        self._area_failed_at = float("-inf")
        self._area_read_mono = float("-inf")
        self._refresh_area()
        self._thread = threading.Thread(target=self._run, name="huesync-engine", daemon=True)
        self._thread.start()

    # -- public ---------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            self._st.desired_sync = True
        self._wake.set()

    def stop(self) -> None:
        with self._lock:
            self._st.desired_sync = False
        self._wake.set()

    def set_intensity(self, level: str) -> None:
        level = (level or "").lower()
        if level not in INTENSITIES:
            raise ValueError("intensity must be one of %s" % ", ".join(INTENSITIES))
        with self._lock:
            self.want_intensity = level
            self._last_sent.pop("set_intensity", None)
        self._wake.set()

    def set_mode(self, mode: str) -> None:
        mode = (mode or "").lower()
        if mode not in MODES:
            raise ValueError("mode must be one of %s" % ", ".join(MODES))
        with self._lock:
            self.want_mode = mode
            self._last_sent.pop("set_app_mode", None)
        self._wake.set()

    def adjust_brightness(self, step: int) -> None:
        self._send_now(build_command("inc_bri", step=int(step)))

    def set_area(self, area_id: str | None) -> None:
        with self._lock:
            self.want_area = area_id or None
        self._wake.set()

    def areas(self) -> list[dict]:
        return hue_sync_groups()

    # -- area switching (overridable for tests) --------------------------------------
    def _refresh_area(self) -> None:
        aid, name = self._read_area()
        self._area_read_mono = time.monotonic()
        self._set(area_id=aid, area_name=name)

    def _read_area(self):
        return hue_sync_selected_area()

    def _write_area(self, area_id: str) -> None:
        hue_sync_write_selected_area(area_id)

    def _kill_app(self) -> bool:
        return hue_sync_kill()

    def _launch_app(self) -> bool:
        return hue_sync_launch(self.launch_exe or hue_sync_exe(), silent=True)

    def _switch_area(self, ws: WebSocket, area_id: str) -> None:
        """Runs in the engine thread; the connection is torn down on purpose."""
        names = {g["id"]: g["name"] for g in self.areas()}
        log.info("switching Hue Sync to entertainment area '%s'", names.get(area_id, area_id))
        self._set(switching=True, error=None)
        try:
            st = self.state()
            if st.state == STATE_SYNCING and self._started_by_us:
                try:
                    ws.send_text(build_command("stop_sync"))
                except OSError:
                    pass
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and self.state().state == STATE_SYNCING:
                    try:
                        ws.settimeout(0.5)
                        self._handle(ws.recv_text())
                    except (socket.timeout, WebSocketClosed, WebSocketError, OSError):
                        break
            self._started_by_us = False
            try:
                ws.close()
            except Exception:
                pass
            self._kill_app()
            time.sleep(1.0)
            self._write_area(area_id)
            if not self._launch_app():
                raise RuntimeError("Hue Sync executable not found - set engine.huesync.launch_exe")
            self._last_launch = time.monotonic()
            self._refresh_area()
        except Exception as e:
            log.error("area switch failed: %s", e)
            self._set(error="area switch failed: %s" % e)
            self._area_failed_at = time.monotonic()
        finally:
            self._set(switching=False)

    def state(self) -> EngineState:
        with self._lock:
            return EngineState(**dict(self._st.__dict__))

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        self._thread.join(timeout=3)

    # -- background ------------------------------------------------------------
    def _run(self) -> None:
        backoff = BACKOFF_MIN_S
        while not self._stop.is_set():
            try:
                ws = WebSocket(self.host, self.port, "/", timeout=self.connect_timeout).connect()
            except (OSError, WebSocketError) as e:
                self._set(connected=False, syncing=False, state=None, error="Hue Sync unreachable: %s" % e)
                self._maybe_launch()
                self._wake.wait(backoff)
                self._wake.clear()
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                continue
            backoff = BACKOFF_MIN_S
            log.info("connected to Hue Sync Public Control at %s:%d (%s)", self.host, self.port, ws.server_header)
            self._ws = ws
            self._last_sent.clear()
            self._refresh_area()
            self._set(connected=True, error=None)
            self._session(ws)
            self._ws = None
            self._set(connected=False, syncing=False, state=None)
            if not self._stop.is_set() and not self._st.switching:
                log.warning("Hue Sync connection lost; reconnecting")

    def _session(self, ws: WebSocket) -> None:
        ws.settimeout(1.0)
        last_ping = time.monotonic()
        while not self._stop.is_set():
            try:
                text = ws.recv_text()
                self._handle(text)
            except socket.timeout:
                pass
            except (WebSocketClosed, WebSocketError, OSError):
                return
            self._reconcile(ws)
            now = time.monotonic()
            if now - last_ping > PING_EVERY_S:
                try:
                    ws.ping()
                except OSError:
                    return
                last_ping = now
        try:
            ws.close()
        except Exception:
            pass

    def _handle(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            log.debug("non-JSON from Hue Sync: %r", text[:200])
            return
        if not isinstance(msg, dict):
            return
        if msg.get("event") == "app_state_update":
            d = msg.get("data") or {}
            st = d.get("state")
            self._set(state=st, mode=d.get("mode"), intensity=d.get("intensity"),
                      bri=d.get("bri"), syncing=(st == STATE_SYNCING), error=None,
                      updated_mono=time.monotonic())
            log.debug("Hue Sync state: %s", d)
        else:
            log.debug("Hue Sync event ignored: %r", text[:200])

    def _reconcile(self, ws: WebSocket) -> None:
        with self._lock:
            st = EngineState(**dict(self._st.__dict__))
            want_sync = st.desired_sync
            want_mode, want_int, want_area = self.want_mode, self.want_intensity, self.want_area
        if st.state is None:
            return                       # no app_state_update yet
        # area first: it restarts the app, everything else follows on reconnect.
        # The user can change the area in the app at any time (it writes the
        # file at once), so never trust the cached value for the decision.
        if (want_area and want_area != st.area_id) or time.monotonic() - self._area_read_mono > 10:
            self._refresh_area()
            st = self.state()
        if want_area and st.area_id and want_area != st.area_id \
                and time.monotonic() - self._area_failed_at > 60:
            self._switch_area(ws, want_area)
            return
        if want_sync:
            if st.state == STATE_DISCONNECTED:
                self._set(error="Hue Sync is not connected to a bridge")
                return
            if st.state != STATE_SYNCING:
                if self._send(ws, "start_sync", build_command("start_sync")):
                    self._started_by_us = True
                return                   # mode/intensity only apply to a live session
            if want_mode and st.mode != want_mode:
                self._send(ws, "set_app_mode", build_command("set_app_mode", mode=want_mode))
                return                   # one step at a time; wait for the state update
            if want_int and st.intensity != want_int:
                self._send(ws, "set_intensity", build_command("set_intensity", intensity=want_int))
        else:
            if st.state == STATE_SYNCING and self._started_by_us:
                self._send(ws, "stop_sync", build_command("stop_sync"))
            elif st.state != STATE_SYNCING:
                self._started_by_us = False

    def _send(self, ws: WebSocket, key: str, text: str) -> bool:
        now = time.monotonic()
        if now - self._last_sent.get(key, float("-inf")) < RESEND_AFTER_S:
            return False
        self._last_sent[key] = now
        try:
            ws.send_text(text)
            log.info("-> Hue Sync %s", text)
            return True
        except OSError as e:
            log.warning("send to Hue Sync failed: %s", e)
            return False

    def _send_now(self, text: str) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("Hue Sync not connected")
        ws.send_text(text)
        log.info("-> Hue Sync %s", text)

    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self._st, k, v)

    def _maybe_launch(self) -> None:
        now = time.monotonic()
        if now - self._warned_absent > 60:
            log.warning("Hue Sync app not reachable on %s:%d (is it running with "
                        "'Allow public control' enabled?)", self.host, self.port)
            self._warned_absent = now
        if not self.launch_exe or now - self._last_launch < 60:
            return
        if not os.path.exists(self.launch_exe):
            return
        self._last_launch = now
        try:
            subprocess.Popen([self.launch_exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL)
            log.info("launched %s", self.launch_exe)
        except OSError as e:
            log.warning("could not launch Hue Sync: %s", e)
