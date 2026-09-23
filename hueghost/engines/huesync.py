r"""Drive the official Hue Sync desktop app over its "Public Control" WebSocket.

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

**Mode and intensity are live and never restart the app.** They have real
commands, so switching from video to music or games mid-movie costs one frame
on the socket and nothing else. Only the settings below can restart anything.

Two settings have no command at all - the entertainment area (``SelectedGroup``
in ``%APPDATA%\HueSync\bridge.json``) and "use audio for light effects"
(``Core.AppMode.<Video|Games>.WithAudio`` in ``config.json``). The app reads
both at start-up, so applying them means: stop sync -> kill the app -> patch
the file(s) -> relaunch ``-silent`` (~2-3 s) -> sync. All of them are patched
in the same restart, and the audio switch is written for **every** mode that
has one, not just the one we are in - so a later video <-> games switch has
nothing left to apply and stays live.

Because that restart is visible (the app's window reappears), the engine only
does it *while it wants sync*, and only once per sync session: outside that,
and after the session's settings have been applied, the app belongs to the
user. The mode is deliberately not part of what a session records as applied
(see ``_apply_startup_settings``), so changing it can never bring the restart
back. ``manage_area=False`` opts out of area control entirely.

Hue Ghost also *mirrors* the app. Change the mode or the intensity in Hue Sync
itself mid-session and the engine adopts it - it stops re-asserting its own
choice and reports the change upwards through ``on_app_change`` so the daemon
can persist it, which is what makes the window, the tray, the API and Home
Assistant all show what the app is really doing. Adoption only happens once the
app has echoed our own wish back at least once this session: the update that
answers ``start_sync`` still carries the app's previous mode, and that is not a
choice anyone made. The area and the audio switch are reported but not adopted;
our own are re-applied at the next session.

The engine only ever stops a sync it started itself, so a sync you start by
hand in the app (a game, say) is left alone.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Callable

from ..winutil import (AUDIO_MODES, hue_sync_app_config, hue_sync_exe, hue_sync_groups,
                       hue_sync_kill, hue_sync_launch, hue_sync_patch,
                       hue_sync_selected_area, hue_sync_write_selected_area,
                       list_audio_outputs, list_displays)
from ..wsclient import WebSocket, WebSocketClosed, WebSocketError
from . import AUTO, Engine, EngineState

log = logging.getLogger("hue-ghost.huesync")

def _display_name(monitor_id: str | None) -> str | None:
    """Friendly name for a monitor DeviceID, for the UI and the logs."""
    if not monitor_id:
        return None
    for d in list_displays():
        if d.monitor_id == monitor_id:
            return d.friendly or d.name
    return None


def _audio_name(endpoint_id: str | None) -> str | None:
    if not endpoint_id:
        return None
    for a in list_audio_outputs():
        if a.id == endpoint_id:
            return a.name
    return None


STATE_DISCONNECTED = "bridge_disconnected"
STATE_CONNECTED = "bridge_connected"
STATE_SYNCING = "syncing"
INTENSITIES = ("subtle", "moderate", "high", "extreme")
MODES = ("video", "games", "music", "scenes")
APP_READ_EVERY_S = 10.0     # re-read the app's own files (the user may change them any time)
APP_RESTART_COOLDOWN_S = 60.0

RESEND_AFTER_S = 2.5        # wait this long for an app_state_update before re-sending a command
BACKOFF_MIN_S, BACKOFF_MAX_S = 1.0, 30.0
PING_EVERY_S = 20.0
SESSION_POLL_S = 0.3        # recv timeout = how quickly a start()/stop() request reaches the app


def build_command(name: str, **data) -> str:
    msg: dict = {"command": name}
    if data:
        msg["data"] = data
    return json.dumps(msg)


class HueSyncEngine(Engine):
    name = "huesync"

    def __init__(self, host: str = "127.0.0.1", port: int = 24851, mode: str = "video",
                 intensity: str = "", use_audio: bool | None = None, manage_area: bool = True,
                 launch_exe: str = "", connect_timeout: float = 3.0,
                 manage_monitor: bool = True, manage_audio_device: bool = True,
                 on_app_change: Callable[[dict], None] | None = None):
        self.host, self.port = host, int(port)
        self.want_mode = (mode or "").lower() or None
        self.want_intensity = (intensity or "").lower() or None
        self.want_use_audio = use_audio if use_audio is None else bool(use_audio)
        self.manage_area = bool(manage_area)
        self.manage_monitor = bool(manage_monitor)
        self.manage_audio_device = bool(manage_audio_device)
        self.launch_exe = launch_exe or ""
        self.connect_timeout = connect_timeout

        self._st = EngineState(name=self.name)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._ws: WebSocket | None = None
        self._last_sent: dict[str, float] = {}
        self._on_app_change = on_app_change
        # mirroring: which commands the app has echoed back at our wanted value
        # this session, and what we have sent but not yet seen answered
        self._asserted: set[str] = set()
        self._inflight: dict[str, tuple[str, float]] = {}
        # monotonic() counts from boot: "never" must be -inf, not 0, or the
        # cooldowns below silently hold for the first minute after start-up
        self._last_launch = float("-inf")
        self._warned_absent = float("-inf")
        self._started_by_us = False       # we sent the start_sync that is running
        self.want_area: str | None = None
        self.want_monitor: str | None = None
        self.want_audio_device: str | None = None
        self._restart_failed_at = float("-inf")
        self._app_read_mono = float("-inf")
        # brightness has no absolute command: a target is turned into inc_bri
        # steps against the level the app reports, once we are connected
        self._bri_target: int | None = None
        self._bri_step = 0
        # what this sync session has already been applied for; None = not applied
        # yet. Compared against (area, audio, mode, monitor, audio device): the
        # app's own state is never part of it, so a change *the user* makes in
        # the app is not fought.
        self._applied: tuple | None = None
        self._refresh_app_state()
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
            self._rewish("set_intensity")
        self._wake.set()

    def set_mode(self, mode: str) -> None:
        mode = (mode or "").lower()
        if mode not in MODES:
            raise ValueError("mode must be one of %s" % ", ".join(MODES))
        with self._lock:
            self.want_mode = mode
            self._rewish("set_app_mode")
        self._wake.set()

    def set_use_audio(self, on: bool | None) -> None:
        """None = leave the app's own "use audio for light effects" alone."""
        with self._lock:
            self.want_use_audio = on if on is None else bool(on)
        self._wake.set()

    def set_manage_area(self, on: bool) -> None:
        with self._lock:
            self.manage_area = bool(on)
        self._wake.set()

    def set_manage_monitor(self, on: bool) -> None:
        with self._lock:
            self.manage_monitor = bool(on)
        self._wake.set()

    def set_manage_audio_device(self, on: bool) -> None:
        with self._lock:
            self.manage_audio_device = bool(on)
        self._wake.set()

    def _rewish(self, key: str) -> None:
        """Our own wish changed: forget that the app ever confirmed the old one.
        Call with the lock held.

        Without this, the next app_state_update - and an inc_bri reply carries
        mode and intensity too - would read as the user picking the *old* value
        in the app, and we would adopt it straight back over their new choice."""
        self._last_sent.pop(key, None)
        self._asserted.discard(key)
        self._inflight.pop(key, None)

    def adjust_brightness(self, step: int) -> None:
        """Nudge the level. Declarative like everything else here: a closed app
        is not an error, the step is simply flushed once it is back."""
        with self._lock:
            self._bri_step += int(step)
        self._wake.set()

    def set_brightness(self, level: int) -> None:
        with self._lock:
            self._bri_target = max(0, min(100, int(level)))
            self._bri_step = 0           # an absolute wish supersedes pending nudges
        self._wake.set()

    def set_area(self, area_id: str | None) -> None:
        with self._lock:
            self.want_area = area_id or None
        self._wake.set()

    def set_monitor(self, monitor: str | None) -> None:
        """Which display the app captures: a monitor DeviceID, AUTO, or None."""
        with self._lock:
            self.want_monitor = monitor or None
        self._wake.set()

    def set_audio_device(self, endpoint: str | None) -> None:
        """Which render endpoint music mode listens to."""
        with self._lock:
            self.want_audio_device = endpoint or None
        self._wake.set()

    def apply_plan(self, plan) -> None:
        """Everything one activity wants, under a single lock: applied one at a
        time, a new area and a new capture display would be two app restarts."""
        mode = (plan.mode or "").lower()
        if mode and mode not in MODES:
            raise ValueError("mode must be one of %s" % ", ".join(MODES))
        level = (plan.intensity or "").lower()
        if level and level not in INTENSITIES:
            raise ValueError("intensity must be one of %s" % ", ".join(INTENSITIES))
        with self._lock:
            if mode and mode != self.want_mode:
                self.want_mode = mode
                self._rewish("set_app_mode")
            if level and level != self.want_intensity:
                self.want_intensity = level
                self._rewish("set_intensity")
            self.want_use_audio = plan.use_audio if plan.use_audio is None else bool(plan.use_audio)
            self.want_area = plan.area_id or None
            self.want_monitor = plan.monitor or None
            self.want_audio_device = plan.audio_device or None
        self._wake.set()

    def areas(self) -> list[dict]:
        return hue_sync_groups()

    # -- the app's own files (overridable for tests) ----------------------------------
    def _refresh_app_state(self) -> None:
        """Re-read what the app itself has selected: area, the audio switch of
        *every* mode that has one, the display it captures and its music input.

        One read of config.json rather than five - the app rewrites that file on
        exit, and separate reads of it can tear."""
        aid, name = self._read_area()
        cfg = self._read_app_config()
        audio_modes = dict(cfg.get("with_audio") or {})
        mon, adev = cfg.get("monitor"), cfg.get("audio_device")
        self._app_read_mono = time.monotonic()
        # audio_modes is *replaced*, never mutated in place: state() is a shallow
        # copy, so mutating it would reach into snapshots already handed out
        self._set(area_id=aid, area_name=name, audio_modes=audio_modes,
                  use_audio=audio_modes.get(self.want_mode or "video"),
                  monitor_id=mon, monitor_name=_display_name(mon),
                  monitor_auto=cfg.get("automatic_display"),
                  audio_device_id=adev, audio_device_name=_audio_name(adev),
                  audio_device_auto=cfg.get("automatic_audio_device"))

    def _read_area(self):
        return hue_sync_selected_area()

    def _read_app_config(self) -> dict:
        return hue_sync_app_config()

    def _write_area(self, area_id: str) -> None:
        hue_sync_write_selected_area(area_id)

    def _write_app_config(self, *, with_audio: dict[str, bool] | None = None,
                          monitor: str | None = None,
                          audio_device: str | None = None) -> None:
        """Every start-up-only setting config.json holds, in one write and one
        digest rewrite - the app is stopped, but it must never see a half state."""
        hue_sync_patch(with_audio=with_audio, monitor=monitor, audio_device=audio_device)

    def _kill_app(self) -> bool:
        return hue_sync_kill()

    def _launch_app(self) -> bool:
        return hue_sync_launch(self.launch_exe or hue_sync_exe(), silent=True)

    def _describe(self, need: dict) -> list[str]:
        """What a restart is for, in the one wording the log and the UI share."""
        what = []
        if need.get("area_id"):
            names = {g["id"]: g["name"] for g in self.areas()}
            what.append("area '%s'" % names.get(need["area_id"], need["area_id"]))
        for mode in AUDIO_MODES:
            if mode in (need.get("audio_modes") or {}):
                what.append("%s audio for %s effects"
                            % ("use" if need["audio_modes"][mode] else "no", mode))
        if need.get("monitor"):
            m = need["monitor"]
            what.append("display %s" % ("chosen by the app" if m == AUTO
                                        else (_display_name(m) or m)))
        if need.get("audio_device"):
            a = need["audio_device"]
            what.append("music input %s" % ("chosen by the app" if a == AUTO
                                            else (_audio_name(a) or a)))
        return what

    def _restart_app(self, ws: WebSocket, *, area_id: str | None = None,
                     audio_modes: dict[str, bool] | None = None,
                     monitor: str | None = None,
                     audio_device: str | None = None) -> bool:
        """Apply the settings the app only reads at start-up. Runs in the engine
        thread; the connection is torn down on purpose. False when it failed.
        Every one of them is applied in this single restart."""
        self._set(switching=True, error=None)
        try:
            need = {"area_id": area_id, "audio_modes": audio_modes,
                    "monitor": monitor, "audio_device": audio_device}
            log.info("restarting Hue Sync to apply %s", " + ".join(self._describe(need)))
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
            if area_id:
                self._write_area(area_id)          # bridge.json: a different file, no digest
            if audio_modes or monitor or audio_device:
                self._write_app_config(with_audio=audio_modes or None,
                                       monitor=monitor, audio_device=audio_device)
            if not self._launch_app():
                raise RuntimeError("Hue Sync executable not found - set engine.huesync.launch_exe")
            self._last_launch = time.monotonic()
            self._refresh_app_state()
            st = self.state()
            # the app reads these at start-up and writes them back from memory;
            # a mismatch here is the early warning that its file format moved
            if area_id and st.area_id != area_id:
                log.warning("Hue Sync still reports area %s after the restart", st.area_id)
            if monitor and monitor != AUTO and st.monitor_id != monitor:
                log.warning("Hue Sync still captures %s after the restart", st.monitor_id)
            if audio_device and audio_device != AUTO and st.audio_device_id != audio_device:
                log.warning("Hue Sync still listens to %s after the restart", st.audio_device_id)
            if monitor == AUTO and st.monitor_auto is not True:
                log.warning("Hue Sync is still pinned to display %s after the restart", st.monitor_id)
            if audio_device == AUTO and st.audio_device_auto is not True:
                log.warning("Hue Sync is still pinned to music input %s after the restart",
                            st.audio_device_id)
            return True
        except Exception as e:
            log.error("Hue Sync restart failed: %s", e)
            self._set(error="Hue Sync restart failed: %s" % e)
            self._restart_failed_at = time.monotonic()
            return False
        finally:
            self._set(switching=False)

    def state(self) -> EngineState:
        with self._lock:
            return EngineState(**dict(self._st.__dict__))

    def alive(self) -> bool:
        return self._thread.is_alive()

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
            self._asserted.clear()        # a reconnect is a new session
            self._inflight.clear()
            self._refresh_app_state()
            self._set(connected=True, error=None)
            try:
                self._session(ws)
            except Exception:
                # never let the worker thread die: a dead thread means no more
                # re-asserts of start_sync / mode / intensity until app restart
                log.exception("engine session crashed - reconnecting")
            self._ws = None
            self._set(connected=False, syncing=False, state=None)
            if not self._stop.is_set() and not self._st.switching:
                log.warning("Hue Sync connection lost; reconnecting")

    def _session(self, ws: WebSocket) -> None:
        ws.settimeout(SESSION_POLL_S)
        last_ping = time.monotonic()
        while not self._stop.is_set():
            try:
                text = ws.recv_text()
                self._handle(text)
            except socket.timeout:
                pass
            except (WebSocketClosed, WebSocketError, OSError):
                return
            try:
                self._reconcile(ws)
            except Exception:
                # keep the connection: the next tick retries the reconcile
                log.exception("reconcile failed - will retry")
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
            with self._lock:
                prev_mode = self._st.mode
            self._set(state=st, mode=d.get("mode"), intensity=d.get("intensity"),
                      bri=d.get("bri"), syncing=(st == STATE_SYNCING), error=None,
                      updated_mono=time.monotonic())
            log.debug("Hue Sync state: %s", d)
            self._adopt(d, st, prev_mode)
        else:
            log.debug("Hue Sync event ignored: %r", text[:200])

    def _adopt(self, d: dict, state: str | None, prev_mode: str | None) -> None:
        """Mirror a mode or intensity the *user* picked in the Hue Sync app.

        Only while syncing. Outside a session the app ignores ``set_app_mode``
        anyway, so there is nothing to fight - and, more importantly, the update
        that answers our own ``start_sync`` still carries the app's *previous*
        mode, which nobody chose. ``_asserted`` is what tells the two apart:
        until the app has echoed our wish back at least once this session, every
        value we see is that stale one. After it has, a value we did not ask for
        is the user at the app's own controls, and they win."""
        if state != STATE_SYNCING:
            with self._lock:
                self._asserted.clear()
                self._inflight.clear()
            return
        adopted: dict = {}
        now = time.monotonic()
        # Hue Sync keeps an intensity *per mode* (Core.AppMode.<Mode>.Default), so
        # every mode switch drags the new mode's preset along with it. That is a
        # consequence of the mode, never somebody choosing an intensity.
        mode_changed = prev_mode is not None and (d.get("mode") or "").lower() != prev_mode.lower()
        with self._lock:
            for field, key, attr in (("mode", "set_app_mode", "want_mode"),
                                     ("intensity", "set_intensity", "want_intensity")):
                v = (d.get(field) or "").lower() or None
                if v is None:
                    continue
                if field == "intensity" and mode_changed:
                    # drop our confirmation instead: the reconcile then puts the
                    # intensity hue-ghost is set to back onto the new mode
                    self._rewish(key)
                    continue
                if v == getattr(self, attr):
                    self._asserted.add(key)          # our wish is live in the app
                    self._inflight.pop(key, None)
                    continue
                if key not in self._asserted:
                    continue                         # the pre-session value, not a choice
                fl = self._inflight.get(key)
                if fl is not None and now - fl[1] < RESEND_AFTER_S:
                    continue                         # our own command is still in flight
                setattr(self, attr, v)               # the user picked it: it wins
                self._inflight.pop(key, None)
                adopted[field] = v
        if adopted:
            log.info("adopting %s from the Hue Sync app", adopted)
            self._notify(adopted)

    def _notify(self, adopted: dict) -> None:
        """Tell the daemon, outside our own lock and never fatally: a listener
        that raises must not take the session thread down with it."""
        cb = self._on_app_change
        if cb is None:
            return
        try:
            cb(dict(adopted))
        except Exception:
            log.exception("on_app_change listener failed")

    def _reconcile(self, ws: WebSocket) -> None:
        with self._lock:
            st = EngineState(**dict(self._st.__dict__))
            want_sync = st.desired_sync
            want_mode, want_int, want_area = self.want_mode, self.want_intensity, self.want_area
            want_audio, manage_area = self.want_use_audio, self.manage_area
            want_mon = self.want_monitor if self.manage_monitor else None
            want_adev = self.want_audio_device if self.manage_audio_device else None
        if st.state is None:
            return                       # no app_state_update yet
        self._flush_brightness(ws, st)
        # The user can change the area or the audio switch in the app at any
        # time; keep reporting what they picked, session or no session.
        if time.monotonic() - self._app_read_mono > APP_READ_EVERY_S:
            self._refresh_app_state()
            st = self.state()
        if not want_sync:
            # Hands off: outside a sync session the app is the user's. Our own
            # settings are re-applied at the start of the next session.
            self._applied = None
            with self._lock:
                self._asserted.clear()
                self._inflight.clear()
            if st.state == STATE_SYNCING and self._started_by_us:
                self._send(ws, "stop_sync", build_command("stop_sync"))
            elif st.state != STATE_SYNCING:
                self._started_by_us = False
            return
        if st.state == STATE_DISCONNECTED:
            self._set(error="Hue Sync is not connected to a bridge")
            return
        # Start-up-only settings first: they restart the app, everything else
        # follows on reconnect.
        if self._apply_startup_settings(ws, want_area if manage_area else None, want_audio,
                                        want_mode, want_mon, want_adev):
            return
        if st.state != STATE_SYNCING:
            if self._send(ws, "start_sync", build_command("start_sync")):
                self._started_by_us = True
            return                       # mode/intensity only apply to a live session
        if want_mode and st.mode != want_mode:
            self._send(ws, "set_app_mode", build_command("set_app_mode", mode=want_mode),
                       value=want_mode)
            return                       # one step at a time; wait for the state update
        if want_int and st.intensity != want_int:
            self._send(ws, "set_intensity", build_command("set_intensity", intensity=want_int),
                       value=want_int)

    def _apply_startup_settings(self, ws: WebSocket, want_area: str | None,
                                want_audio: bool | None, want_mode: str | None,
                                want_monitor: str | None = None,
                                want_audio_device: str | None = None) -> bool:
        """Point the app at the area / audio switch / capture display / music
        input this sync session wants. Returns True when the app was restarted
        (the connection is gone).

        Done once per session: afterwards ``_applied`` records what we asked
        for, so a change the *user* then makes in the app stands until our
        wishes themselves change.

        ``want_mode`` is deliberately **not** part of that key. The mode has a
        live command, so changing it must never reach this function at all; it
        is only an input to the decision here (music captures no display).
        """
        want = (want_area, want_audio, want_monitor, want_audio_device)
        if self._applied == want:
            return False
        if time.monotonic() - self._restart_failed_at < APP_RESTART_COOLDOWN_S:
            return False                 # checked first: the reads below are not free
        # the user may have changed any of them in the app since we last looked
        self._refresh_app_state()
        need = self._needed(self.state(), want, (want_mode or "video"))
        if not any(need.values()):
            self._applied = want         # nothing to do: this session is set up
            return False
        if self._restart_app(ws, **need):
            self._applied = want         # one restart per session; a failed one
        return True                      # is retried after the cooldown

    def _needed(self, st: EngineState, want: tuple, mode: str) -> dict:
        """What still has to be written to make ``want`` true; empty values mean
        the app is already there. Keeping the diff in one place is what stops a
        wish the app already satisfies from costing a restart."""
        want_area, want_audio, want_monitor, want_audio_device = want
        need: dict = {"area_id": None, "audio_modes": None,
                      "monitor": None, "audio_device": None}
        if want_area and st.area_id and want_area != st.area_id:
            need["area_id"] = want_area
        if want_audio is not None:
            # every mode that *has* an audio switch, not just the one we are in:
            # writing them together is what lets a later video <-> games switch
            # happen over the socket, with no restart at all
            diff = {m: want_audio for m in AUDIO_MODES
                    if st.audio_modes.get(m) is not None and st.audio_modes.get(m) != want_audio}
            need["audio_modes"] = diff or None
        # music mode captures nothing, so a music session must not drag the
        # display along - that would be a restart for a setting it cannot use
        if want_monitor and mode != "music":
            if want_monitor == AUTO:
                # AUTO asks the app to choose for itself, so only its own flag can
                # satisfy it; comparing it to a monitor id made every single
                # reconcile look dirty, and restarted the app on every mode change
                if st.monitor_auto is not True:
                    need["monitor"] = AUTO
            elif st.monitor_id != want_monitor or st.monitor_auto is True:
                need["monitor"] = want_monitor
        if want_audio_device:
            if want_audio_device == AUTO:
                if st.audio_device_auto is not True:
                    need["audio_device"] = AUTO
            elif st.audio_device_id != want_audio_device or st.audio_device_auto is True:
                need["audio_device"] = want_audio_device
        return need

    def _flush_brightness(self, ws: WebSocket, st: EngineState) -> None:
        """Turn a wished-for level into the signed step the protocol has. Only
        possible once the app has told us where it is."""
        with self._lock:
            target, step = self._bri_target, self._bri_step
        if target is None and not step:
            return
        if target is not None:
            if st.bri is None:
                return                   # no app_state_update with a level yet; wait
            step = target - int(st.bri)
        with self._lock:
            self._bri_target, self._bri_step = None, 0
        if step:
            self._send_now(build_command("inc_bri", step=int(step)))

    def _send(self, ws: WebSocket, key: str, text: str, value: str | None = None) -> bool:
        now = time.monotonic()
        if now - self._last_sent.get(key, float("-inf")) < RESEND_AFTER_S:
            return False
        self._last_sent[key] = now
        try:
            ws.send_text(text)
            log.info("-> Hue Sync %s", text)
            if value is not None:
                # remember what we asked for: the answer confirms it, anything
                # else arriving later is the user, not us
                self._inflight[key] = (value, now)
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
            # -silent: start minimised to the tray, no window on the user's screen
            if hue_sync_launch(self.launch_exe, silent=True):
                log.info("launched %s -silent", self.launch_exe)
        except OSError as e:
            log.warning("could not launch Hue Sync: %s", e)
