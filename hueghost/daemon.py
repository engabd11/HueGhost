"""The orchestrator: watch the followed Jellyfin session, keep the ghost in
lockstep, and turn the light engine on/off at the right moments.

States:  idle -> ghosting (mpv up, waiting for / has frames) -> syncing (engine reports sync)
         -> standby (lights off on purpose, ghost paused and kept alive)

Ordering rules (avoid flashing the desktop colours into the living room):
  start: wake the display -> launch mpv -> first time-pos seen -> engine.start()
  stop:  lights off after ``sync.lights_off_delay_s`` (ghost holds) -> close mpv after
         ``sync.idle_stop_delay_s``; a client that comes back in between gets its lights
         re-asserted without an mpv relaunch.
"""
from __future__ import annotations

import copy
import dataclasses
import logging
import os
import queue
import statistics
import subprocess
import threading
import time
from collections import deque

from . import __version__
from .config import Config, INTENSITIES, KEEP_AWAKE_MODES, MODES, _deep_merge, app_data_dir, source_root
from .control import ControlServer
from .engines import Engine, build_engine
from .ghost import GhostPlayer, mpv_args
from .jellyfin import JellyfinClient, JellyfinError
from .lockstep import GhostObs, Params, Pause, Resume, Seek, decide
from .screencare import ScreenCare
from .sources import IDLE as IDLE_ACTIVITY, build_sources
from .watcher import Observation, PlayerSet, SessionWatcher
from .winutil import desktop_locked, keep_awake, wake_display

log = logging.getLogger("hue-ghost")

IDLE, GHOSTING, SYNCING, STANDBY = "idle", "ghosting", "syncing", "standby"
TICK_S = 0.25
ENGINE_STOP_WAIT_S = 2.0
SLOW_STEP_S = 2.0          # a loop step holding the lock longer than this is logged
BINDING_FIELDS = ("area_id", "mode", "intensity", "use_audio")  # what /set may change per source
MUSIC_SEEK_COOLDOWN_S = 2.0
MUSIC_SEEK_CHECK_S = 1.0    # a music seek is judged this long after it was sent
MUSIC_SETTLE_S = 5.0        # ... once a file has played this long (before, seeks wait on the network)
MUSIC_PRECISE_JITTER_S = 0.03  # a music player scattering less than this locks after LOCK_SETTLE_REPORTS ...
MUSIC_NOISY_SETTLE_REPORTS = 6  # ... a noisier one after this many reports agree
# time lock: engage once |drift| is this small ("0.0 s"), the
# followed client has sent this many quiet reports since its last event ...
LOCK_WINDOW_S = 0.02
LOCK_SETTLE_REPORTS = 3
LOCK_AGREE_S = 0.02     # ... and its recent timed reports agree with the timeline this well


def _asc(s) -> str:
    return (s or "").encode("ascii", "replace").decode("ascii")


def _get(d: dict, dotted: str):
    for p in dotted.split("."):
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def _tri_state(v) -> bool | None:
    """None / "" / "auto" = leave it to the app; anything else is a bool."""
    if v is None or (isinstance(v, str) and v.strip().lower() in ("", "auto", "app", "none")):
        return None
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def audio_endpoint_present(endpoint_id: str) -> bool:
    """Whether the render endpoint the ghost's music plays into exists right now.

    HDMI/DP audio rides on its display: the display goes to sleep and the
    endpoint disappears with it - and mpv then dies (rc=2) the moment it opens
    a file pinned to the missing device. Best effort: True when we cannot tell
    (not Windows, audio service stopped, old endpoint-id shape)."""
    try:
        from .winutil import list_audio_outputs

        outputs = list_audio_outputs() or []
    except Exception:
        return True
    want = str(endpoint_id).rsplit(".", 1)[-1].strip().strip("{}").lower()
    return any(str(getattr(o, "guid", "") or "").strip("{}").lower() == want
               for o in outputs)


def pause_stop_due(paused_since: float | None, now: float, limit_s: float,
                   already_stopped: bool) -> bool:
    """True when sync should stop because the followed client has been paused
    for ``limit_s`` seconds (0 disables the feature)."""
    if already_stopped or paused_since is None or limit_s <= 0:
        return False
    return now - paused_since >= limit_s


class DriftStats:
    """Per-minute summary of ghost-vs-target drift, so sync quality is measured."""

    def __init__(self, period_s: float = 60.0):
        self.period = period_s
        self.samples: list[float] = []
        self.seeks = 0
        self.nudge_ticks = 0
        self.locked_ticks = 0
        self.started = time.monotonic()
        self.current: float | None = None
        self.last_summary: dict | None = None

    def add(self, drift: float, nudging: bool, locked: bool = False) -> None:
        self.current = drift
        self.samples.append(drift)
        if nudging:
            self.nudge_ticks += 1
        if locked:
            self.locked_ticks += 1

    def due(self, now: float) -> bool:
        return now - self.started >= self.period

    def flush(self) -> dict | None:
        if not self.samples:
            self.started = time.monotonic()
            self.seeks = 0
            self.nudge_ticks = 0
            self.locked_ticks = 0
            return None
        absd = sorted(abs(d) for d in self.samples)
        p95 = absd[min(len(absd) - 1, int(round(0.95 * (len(absd) - 1))))]
        summary = {
            "n": len(absd),
            "mean_abs": round(statistics.fmean(absd), 3),
            "p95_abs": round(p95, 3),
            "max_abs": round(absd[-1], 3),
            "mean": round(statistics.fmean(self.samples), 3),
            "seeks": self.seeks,
            "nudge_ticks": self.nudge_ticks,
            "locked_ticks": self.locked_ticks,
        }
        self.last_summary = summary
        self.samples = []
        self.seeks = 0
        self.nudge_ticks = 0
        self.locked_ticks = 0
        self.started = time.monotonic()
        return summary


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sources = build_sources(cfg)
        self.params = Params.from_config(cfg)
        # what the engine's own app changed under us, drained on the next tick:
        # Queue.put never blocks, so the engine thread never waits on our lock
        self._engine_events: queue.Queue[dict] = queue.Queue()
        self.engine: Engine = build_engine(cfg, on_app_change=self._engine_events.put)
        self.ghost: GhostPlayer | None = None
        self.enabled = bool(cfg.get("enabled", True))
        self.state = IDLE
        self.stats = DriftStats()
        self.last_obs: Observation | None = None
        self.last_act = IDLE_ACTIVITY
        self._idle_watcher = None
        self._bindings_snapshot = cfg.bindings()
        self.jf_ok = False
        self.jf_error: str | None = None
        self._jf_err_log = float("-inf")
        self._required_log = float("-inf")
        self._idle_since: float | None = None
        self._paused_since: float | None = None
        # lights switched off on purpose while the ghost lives on: "stopped"
        # (client left, ghost on standby) or "paused" (pause timeout)
        self._standby: str | None = None
        self._awake = ""              # what we hold awake: "" | "system" (music) | "display"
        self.care = ScreenCare(cfg)   # ... and what keeps an OLED held awake from burning in
        self._locked_warned = False
        self._launches: deque[float] = deque()
        self._last_launch = 0.0
        self._last_launch_item: str | None = None
        self._guard_logged = False
        self._pending_seek = False
        self._engine_started = False
        # the Plan the engine was last pointed at, and which binding the live
        # ghost belongs to: both are how a change of winner gets noticed
        self._plan_applied = None
        self._ghost_binding: str | None = None
        self._parked_since: float | None = None  # ... and since when another one has the lights
        self._pause_stopped: str | None = None   # item stopped for sitting paused
        self._eof_item: str | None = None
        self._ended_since: float | None = None   # an idle music ghost finished its track then
        self._adev_warned: str | None = None
        self._adev_woke = float("-inf")
        self._music_gap = False        # a music ghost ran out: the engine waits for the next song
        self._last_report_count = 0
        self._stalls_saved_mono = float("-inf")
        self._startup_latency = 1.0   # EMA of launch -> first time-pos
        self._launch_mono = 0.0
        # music, learned and saved: how long the next song's loadfile takes to
        # play (live: ~0.5 s - the old constant 0.25 opened every song behind),
        # and how far an exact seek lands short of where it was aimed
        self._swap_latency = min(max(float(cfg.get("sync.swap_latency_s", 0.5) or 0.5), 0.0), 3.0)
        self._seek_lead = min(max(float(cfg.get("sync.music_seek_lead_s", 0.0) or 0.0), 0.0), 0.3)
        self._music_learned = False
        self._areas_cache: list[dict] | None = None
        self._areas_read = float("-inf")
        self._swap_opened: tuple[float, float] | None = None   # (mono, pos) a swap opened at
        self._seek_check: float | None = None    # when to look where the last music seek landed
        self._music_settled_at = float("-inf")  # music seeks before this (a file's first seconds) teach nothing
        self._ghost_audio_only = False
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.control: ControlServer | None = None
        self._setup_log = float("-inf")
        self._test_proc: subprocess.Popen | None = None
        self.restart_required: list[str] = []
        self.restart_requested = False
        base = source_root() or app_data_dir()
        os.makedirs(base, exist_ok=True)
        self._mpv_err = os.path.join(base, "mpv_ghost.err")

    # -- setup ----------------------------------------------------------------
    @property
    def _jelly(self):
        """The Jellyfin source, when one is configured."""
        return next((s for s in self.sources.sources if s.id == "jellyfin"), None)

    @property
    def _pc(self):
        return next((s for s in self.sources.sources if s.id == "pc"), None)

    # The daemon used to own these two directly. Keeping them as properties is
    # what lets every existing caller - status(), the CLI, the web API and the
    # virtual-clock tests - go on using `d.watcher` and `d.jf` unchanged.
    @property
    def jf(self):
        s = self._jelly
        return s.client if s is not None else JellyfinClient(self.cfg.get("jellyfin.url"),
                                                             self.cfg.get("jellyfin.api_key"))

    @property
    def watcher(self):
        s = self._jelly
        if s is not None:
            return s.watcher
        if self._idle_watcher is None:      # PC-only install: nothing follows Jellyfin
            self._idle_watcher = SessionWatcher(PlayerSet.from_players([]))
        return self._idle_watcher

    @watcher.setter
    def watcher(self, w) -> None:
        s = self._jelly
        if s is not None:
            s.watcher = w
        else:
            self._idle_watcher = w

    def _rebuild_sources(self, keep_client: bool = True) -> None:
        """Rebuild after a config change, carrying the learned client buffering
        over - it is expensive to relearn and has nothing to do with bindings."""
        stalls = getattr(self.watcher, "stalls", None)
        profiles = getattr(self.watcher, "profiles", None)
        old = self._jelly
        self.sources.close()
        self.sources = build_sources(self.cfg,
                                     jf_client=(old.client if (old and keep_client) else None))
        self._bindings_snapshot = self.cfg.bindings()
        s = self._jelly
        if s is not None and stalls is not None:
            s.watcher.stalls = stalls
        if s is not None and profiles:
            s.watcher.profiles.update(profiles)

    def _start_control(self) -> None:
        c = self.cfg.section("control")
        port = int(c.get("port") or 0)
        if not port:
            return
        try:
            from .webapi import WebApi
            api = WebApi(self)
            self.control = ControlServer(c.get("bind", "127.0.0.1"), port, c.get("token", ""),
                                         self.status, self.action, api.handle)
            self.control.start()
            self.restart_required = []
        except OSError as e:
            log.error("control API unavailable on port %s (another Hue Ghost, or another app, "
                      "holds it - Home Assistant cannot reach this copy): %s", port, e)
            self.control = None

    # -- main loop --------------------------------------------------------------
    def run(self) -> None:
        self._start_control()
        binds = self.cfg.enabled_bindings()
        log.info("hue-ghost %s following %s (engine=%s, poll %.2fs, offset %+.2fs)",
                 __version__,
                 ", ".join("%s -> %s" % (b["name"] or b["id"], b["area_name"] or "current area")
                           for b in binds) or "nothing",
                 self.engine.name,
                 float(self.cfg.get("jellyfin.poll_interval_s", 0.5)), self.offset)
        next_poll = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_poll:
                    with self._lock:
                        self._poll(now)
                    self._slow_step("poll", now)
                    next_poll = now + float(self.cfg.get("jellyfin.poll_interval_s", 0.5))
                t0 = time.monotonic()
                with self._lock:
                    self._tick(time.monotonic())
                    self._sync_power()
                    self._screen_care(time.monotonic())
                self._slow_step("tick", t0)
                wait = max(0.05, min(TICK_S, next_poll - time.monotonic()))
                self._stop.wait(wait)
        finally:
            self._shutdown()

    def keep_awake_mode(self) -> str:
        mode = str(self.cfg.get("ghost.keep_awake", "playing") or "off").lower()
        return mode if mode in KEEP_AWAKE_MODES else "playing"

    def _sync_power(self) -> None:
        """Hold the displays (and the PC) awake while the ghost plays. Windows
        switches every display off after the idle timeout - the virtual ghost
        display included - and Hue Sync then captures a dead screen. The
        execution-state flag is per thread, so this only ever runs on the
        daemon loop thread."""
        mode = self.keep_awake_mode()
        # Music has no picture: the lights ride on sound, so the screens may
        # sleep - only the PC is held awake. That covers a music ghost, the gap
        # between two of its songs (no ghost for a moment, engine still on -
        # this used to read as "a PC app is playing" and woke the monitor at
        # every track change) and a music app on this PC.
        music = (self._ghost_audio_only and (self.ghost is not None or self._music_gap)) \
            or (self._plan_applied is not None and self._plan_applied.mode == "music")
        lit = self.ghost is not None or self._engine_started
        if mode == "always":
            want = "display"
        elif mode == "playing" and lit:
            want = "system" if music else "display"
        else:
            want = ""
        if want == self._awake:
            return
        if keep_awake(bool(want), display=(want == "display")):
            log.info({"display": "displays held awake %s" % ("(always)" if mode == "always"
                                                            else "while the ghost plays"),
                      "system": "music: the PC is held awake, the displays may sleep",
                      "": "displays may sleep again"}[want])
        self._awake = want

    def _screen_care(self, now: float) -> None:
        """Keep a held-awake OLED from showing one picture all night: shift the
        ghost's picture now and then, and black out the displays Hue Sync does
        not capture once nobody is using the PC. Music has no picture to care for."""
        g = self.ghost
        video = g if (g is not None and not self._ghost_audio_only and g.alive()) else None
        spare = set()
        if video is not None:
            captured = self.engine.state().monitor_id
            if captured:
                spare.add(captured)
        self.care.tick(now, video, spare)

    def stop(self) -> None:
        self._stop.set()

    def _slow_step(self, what: str, started: float) -> None:
        """Everything else waits while the loop holds the lock (the control
        API, Home Assistant, the window's saves): say so when a step is slow,
        so a stall has a name in the log."""
        took = time.monotonic() - started
        if took > SLOW_STEP_S:
            log.warning("daemon %s step held the lock for %.1f s (state %s)", what, took, self.state)

    def request_restart(self) -> None:
        """Stop the loop; the front end (cli/tray) re-launches the process."""
        self.restart_requested = True
        log.info("restart requested from the UI")
        threading.Timer(0.5, self._stop.set).start()

    def _shutdown(self) -> None:
        with self._lock:
            self.care.close()             # first: nothing below may leave a display blacked out
            if self.ghost is not None:
                self._stop_ghost("daemon exit")
            try:
                self.engine.stop()
                self.engine.wait_until(lambda s: not s.syncing or not s.connected, ENGINE_STOP_WAIT_S)
            finally:
                self.engine.close()
            if self._awake:
                keep_awake(False)
                self._awake = ""
        if self.control:
            self.control.stop()
        log.info("stopped")

    @property
    def offset(self) -> float:
        """The film offset - or, while a music ghost plays, the music one
        (its own path: no picture, Hue Sync's audio analysis instead)."""
        return self.offset_for(self._ghost_audio_only)

    def offset_for(self, music: bool) -> float:
        if music:
            mo = self.cfg.get("sync.music_offset_s")
            if isinstance(mo, (int, float)):
                return float(mo)
        return float(self.cfg.get("sync.offset_s", 0.0))

    # -- polling the followed session --------------------------------------------
    def _poll(self, now: float) -> None:
        self._drain_engine_events()
        problems = self.cfg.problems()
        if problems:
            self.jf_ok, self.jf_error = False, "setup required: " + problems[0]
            if now - self._setup_log > 120:
                log.warning("setup required (%s) - open the UI at %s", problems[0], self.ui_url())
                self._setup_log = now
            return
        pc = self._pc
        if pc is not None:
            pc.ignore_pid(self.ghost.proc.pid if (self.ghost and self.ghost.proc) else None,
                          *self.care.cover_pids())
        act = self.sources.poll(now)
        jelly = self._jelly
        if jelly is not None:
            if jelly.ok:
                if not self.jf_ok:
                    log.info("Jellyfin reachable")
                self.jf_ok, self.jf_error = True, None
            else:
                self.jf_ok, self.jf_error = False, jelly.error
                if now - self._jf_err_log > 30:
                    log.warning("Jellyfin unreachable (%s) - keeping ghost as-is", _asc(str(jelly.error)))
                    self._jf_err_log = now
                if act is self.last_act:
                    return               # nothing new to act on; leave the ghost alone
        self.last_act = act
        obs = act.obs
        self.last_obs = obs

        if not self.engine.alive():
            log.error("light engine thread died - rebuilding it")
            try:
                self.engine.close()
            except Exception:
                pass
            self.engine = build_engine(self.cfg, on_app_change=self._engine_events.put)
            self._engine_started = False
            self._push_engine_prefs()
            self._plan_applied = None
            if act.playing:
                self._apply_plan(act)

        if obs is not None and obs.model is not None and obs.model.last_debug \
                and log.isEnabledFor(logging.DEBUG):
            if obs.model.reports != self._last_report_count or act.event == "new_item":
                log.debug("%s", obs.model.last_debug)
                obs.model.last_debug = ""
            self._last_report_count = obs.model.reports
        self._persist_stalls(now)

        if act.playing and self.enabled and not self._waits_for_play(act):
            self._playing(now, act)
        else:
            self._idle(now, act)

    def _waits_for_play(self, act) -> bool:
        """A paused source never STARTS the lights - only one that plays does.

        A paused item with no ghost of its own (Hue Ghost starting up beside a
        phone left on a paused song, or a paused player becoming the top
        source when the one above it stops) is treated like nothing playing:
        whatever was lit stops as it would for any stop, and the source lights
        up the moment it plays. One that pauses while its ghost runs is not
        affected: that is a pause, with its own limits."""
        if not act.needs_ghost or act.obs is None or act.obs.model is None:
            return False
        m = act.obs.model
        if not m.paused:
            return False
        if self.ghost is not None and self.ghost.item_id == m.item_id:
            return False
        if self._pause_stopped != m.item_id:
            # also the latch _maybe_launch honours, and _playing clears on play
            self._pause_stopped = m.item_id
            obs = act.obs
            log.info("'%s' is paused on %s - the lights wait for it to play", _asc(m.name),
                     _asc(obs.report.device_label if obs.report else "the client"))
        return True

    def _playing(self, now: float, act) -> None:
        """Something is playing. What that costs us depends on where it is: a
        client on the network has to be mirrored, an app on this PC does not."""
        self._hand_over(now, act)
        self._apply_plan(act)
        if not act.needs_ghost:
            self._follow_ghostless(now, act)
            return
        obs, m = act.obs, act.obs.model
        if self._idle_since is not None:
            self._idle_since = None
            if self.ghost is not None and self._standby == "stopped" and self.ghost.item_id == m.item_id:
                self._lights_on("followed client is back")
        if m.paused and self.ghost is not None:
            if self._paused_since is None:
                self._paused_since = now
        elif self._paused_since is not None or self._standby == "paused":
            if self._standby == "paused" and self.ghost is not None:
                self._lights_on("client resumed after a long pause")
            self._paused_since = None
        if self._pause_stopped is not None and (self._pause_stopped != m.item_id or not m.paused):
            self._pause_stopped = None      # it plays again, or moved on: stop latching
        if act.event == "new_item" and obs.stale:
            log.warning("first report for '%s' is stale (>30 s old); position may be off", _asc(m.name))
        if self.ghost is not None and getattr(self.ghost, "ended", False) \
                and self.ghost.item_id == m.item_id == self._eof_item and m.runtime_s \
                and m.position_at(now) <= m.runtime_s - 5.0:
            # the idle music ghost finished this track, and the client plays it
            # again (repeat one, or seeked back): load it again
            if not self._swap_ghost(now, act, m):
                self._stop_ghost("track replayed")
        swapped = False
        if self.ghost is not None and self.ghost.item_id != m.item_id:
            swapped = self._swap_ghost(now, act, m)
            if not swapped:
                log.info("followed client switched to '%s' -> relaunching ghost", _asc(m.name))
                self._stop_ghost("item changed")
        if self.ghost is None:
            self._maybe_launch(now, act)
        elif swapped and act.event == "new_item":
            pass                        # the swap opened the new song on its anchor
        elif act.event in ("seek", "resume", "new_item", "resync"):
            self._pending_seek = True
            if act.event == "seek":
                log.info("followed client seeked to %.1fs (holding %.1fs for buffering)",
                         m.anchor_pos, max(0.0, m.anchor_mono - now))
            elif act.event == "resync":
                log.info("client resumed earlier/later than predicted -> resync to %.1fs", m.position_at(now))
        elif act.event == "stalled":
            log.info("followed client still buffering at %.1fs", m.anchor_pos)
        elif act.event == "lift":
            if m.last_lift > 0:
                log.info("'%s': the player's reports caught up %+.2fs (it lags, then steps) -> the "
                         "timeline follows the top of its reports", _asc(m.name), m.last_lift)
            else:
                log.info("'%s': the top of the player's reports settled %+.2fs lower -> timeline "
                         "follows it", _asc(m.name), m.last_lift)
        elif act.event == "unlock":
            log.info("time lock released: the client's reports sit %+.2fs off the locked timeline "
                     "-> correcting again", m.lock_residual or 0.0)

    def _apply_plan(self, act) -> None:
        """Point the engine at what the *winning* binding wants: its mode,
        intensity, entertainment area, capture display and music input.

        Whenever the plan changes - not only when a source reports a new item.
        The binding that drives the lights can change without either source
        seeing anything new (an app on this PC taking over from a client that
        is still connected), and that used to leave Hue Sync in the previous
        source's mode and area for the whole session.

        A mode or intensity changed by hand mid-session is not undone here:
        ``act.plan`` only changes when the binding or its settings do."""
        if act.plan == self._plan_applied:
            return
        b = act.binding or {}
        log.info("'%s' drives the lights: %s, %s%s", _asc(b.get("name") or b.get("id") or "?"),
                 act.plan.mode or "the app's own mode",
                 act.plan.intensity or "the app's own intensity",
                 (" -> " + _asc(b.get("area_name"))) if b.get("area_name") else "")
        self.engine.apply_plan(act.plan)
        self._plan_applied = act.plan

    def _swap_ghost(self, now: float, act, m) -> bool:
        """A music source moved on to the next track: swap the file the live
        ghost plays instead of tearing the whole session down.

        The music ghost is invisible - the lights ride on the audio endpoint,
        not on a picture - so stopping the engine for every track only made
        the lights blink off and on between songs. False when a swap is not
        possible (video ghost, another binding won, mpv has gone) and the
        caller must relaunch."""
        spec = act.ghost
        g = self.ghost
        if g is None or not self._ghost_audio_only or spec is None or not spec.audio_only:
            return False
        if self._ghost_binding != ((act.binding or {}).get("id") or None):
            return False              # a different binding won: give it a fresh ghost
        # open the new file where the client is by the time it plays: no
        # process to start, only a loadfile - whose latency is learned
        held = m.paused or m.frozen(now)
        start = m.position_at(now) + self.offset + (0.0 if held else self._swap_latency)
        if m.runtime_s:
            start = min(start, max(0.0, m.runtime_s - 1.0))
        if not g.alive() or not g.swap(spec.url, start=max(0.0, start)):
            return False              # mpv has gone: relaunch
        g.item_id = m.item_id
        self._last_launch_item = m.item_id
        self._eof_item = None
        self._ended_since = None
        self._music_gap = False
        self._paused_since = None
        if self._standby is not None:
            self._lights_on("the next track is here")
        # Land the song exactly, at once: the first position mpv reports gets a
        # seek to the target. Nobody hears the music ghost, so the seek costs
        # nothing - while leaving the rest to the lockstep cost a 0.5 s start
        # closed at 4 % fast tempo over 17 s (live, 2026-10-19).
        self._pending_seek = not held
        self._swap_opened = None if held else (now, max(0.0, start))
        self._music_settled_at = now + MUSIC_SETTLE_S
        log.info("'%s' playing on %s -> hot-swapping the track (lights stay on)",
                 _asc(m.name), _asc((act.binding or {}).get("name") or "?"))
        return True

    def _hand_over(self, now: float, act) -> None:
        """A different binding won: park the ghost the old one left behind, and
        close it once the standby window is over.

        Only ``_idle`` ever closed a ghost, and it never runs while something
        else is playing - so an app on this PC taking the lights over left the
        previous client's mpv running for as long as that app kept playing.

        Parked rather than killed, for the same reason a client that stops is:
        a source that has the lights for a moment must not cost a relaunch and
        a re-sync. Nothing captures the parked ghost, so it holds its frame."""
        bid = (act.binding or {}).get("id") or None
        if self.ghost is None or self._ghost_binding is None or bid == self._ghost_binding:
            if self._parked_since is not None:
                self._parked_since = None
                self._pending_seek = True   # it stood still while the other source played
            return
        if act.needs_ghost:
            # another client, which needs a ghost of its own: the item check in
            # ``_playing`` already reuses this one or relaunches it
            self._ghost_binding = bid
            self._parked_since = None
            return
        if self._parked_since is None:
            self._parked_since = now
            log.info("'%s' took the lights over -> parking the ghost it left behind",
                     _asc(act.title or bid or "another source"))
            if not self.ghost.paused:
                self.ghost.apply([Pause()], now)
        if now - self._parked_since >= float(self.cfg.get("sync.idle_stop_delay_s", 10.0)):
            self._stop_ghost("another source has the lights")

    def _follow_ghostless(self, now: float, act) -> None:
        """An app on this PC: nothing to launch, nothing to keep in step - the
        picture is already on a screen Hue Sync captures. Just light it."""
        self._idle_since = None
        self._paused_since = None
        self._music_gap = False        # an app has the lights now, not a song in between tracks
        if self._standby is not None:
            self._lights_on("%s is playing again" % (act.title or "the app"))
        if not self._engine_started:
            log.info("%s is playing on this PC -> engine start", _asc(act.title or "an app"))
            self.engine.start()
            self._engine_started = True
        est = self.engine.state()
        self.state = SYNCING if est.syncing else GHOSTING

    def _idle(self, now: float, act) -> None:
        self._parked_since = None       # nothing else has the lights any more
        if self.ghost is not None:
            if self._idle_since is None:
                self._idle_since = now
                log.info("followed client %s", "stopped" if act.seen else "gone")
            idle = now - self._idle_since
            close_after = float(self.cfg.get("sync.idle_stop_delay_s", 10.0))
            off_after = self.music_gap if self._ghost_audio_only else self.lights_off_delay
            if self._standby != "stopped" and idle >= off_after:
                self._lights_off("stopped", now)
                log.info("lights off - TV stopped %.1fs ago; ghost on standby for %.0fs in case it comes back",
                         idle, max(0.0, close_after - idle))
            if idle >= close_after:
                self._stop_ghost("followed client idle")
        elif self._engine_started:
            # a PC source stopped: there is no ghost to park, only lights to stop
            if self._idle_since is None:
                self._idle_since = now
                log.info("music stopped (waiting %.0fs for the next song)" % self.music_gap
                         if self._music_gap else "nothing playing on this PC any more")
            if now - self._idle_since >= (self.music_gap if self._music_gap else self.lights_off_delay):
                self.engine.stop()
                self._engine_started = False
                self._standby = None
                self._idle_since = None
                self._music_gap = False
                self._plan_applied = None   # the next session re-asserts the binding's settings
                self.state = IDLE
                log.info("lights off")
        elif not self.enabled and self.state != IDLE:
            self.state = IDLE

    @property
    def lights_off_delay(self) -> float:
        return max(0.0, float(self.cfg.get("sync.lights_off_delay_s", 1.5) or 0.0))

    @property
    def music_gap(self) -> float:
        """How long music may stop between songs before the lights go off -
        never shorter than the lights-off delay everything else gets."""
        return max(self.lights_off_delay, float(self.cfg.get("sync.music_gap_s", 10.0) or 0.0))

    def _lights_off(self, reason: str, now: float) -> None:
        """Stop the light engine but keep the ghost alive (paused), so a client
        that comes straight back does not cost an mpv relaunch and a re-sync."""
        if self._standby is None:
            self.engine.stop()
            if self.ghost is not None and not self.ghost.paused:
                self.ghost.apply([Pause()], now)
        self._standby = reason

    def _lights_on(self, why: str) -> None:
        log.info("%s -> lights back on", why)
        self._standby = None
        self._pending_seek = True
        if self._engine_started:
            self.engine.start()

    def _refund_launch(self, g, now: float) -> None:
        """A ghost that ran for >= 30 s proves its environment works: forget its
        launch (and any older ones), or one rough patch of rc=2 deaths keeps
        the rate guard blocking relaunches - and the next track with them -
        for minutes afterwards."""
        started = getattr(g, "started_mono", None)
        if started is not None and now - started >= 30 and not g.user_quit:
            self._launches.clear()

    def _persist_stalls(self, now: float) -> None:
        """Save what was learned about the clients: their buffering, how their
        reports err (player profiles) and the music ghost's own latencies."""
        st = self.watcher.stalls
        w = self.watcher
        profiles = hasattr(w, "profiles_changed") and w.profiles_changed()
        if not (st.changed or profiles or self._music_learned) \
                or now - self._stalls_saved_mono < 60 or not self.cfg.path:
            return
        self._stalls_saved_mono = now
        said = []
        if st.changed:
            st.changed = False
            self.cfg.set("sync.stall_estimates", dict(st.est))
            said.append("client buffering %s" % {k: round(v, 1) for k, v in st.est.items()})
        if profiles:
            dump = w.dump_profiles()
            self.cfg.set("sync.player_profiles", dump)
            said.append("players %s" % ", ".join("%s %s" % (dev[:12], p["kind"]) for dev, p in dump.items()))
        if self._music_learned:
            self._music_learned = False
            self.cfg.set("sync.swap_latency_s", round(self._swap_latency, 3))
            self.cfg.set("sync.music_seek_lead_s", round(self._seek_lead, 3))
            said.append("music swap %.2fs, seek lead %.3fs" % (self._swap_latency, self._seek_lead))
        try:
            self.cfg.save()
            log.info("learned: %s", "; ".join(said))
        except OSError as e:
            log.warning("could not persist what was learned: %s", e)

    def _maybe_launch(self, now: float, act) -> None:
        obs = act.obs
        m = obs.model
        assert m is not None
        spec = act.ghost
        g = self.cfg.section("ghost")
        if bool(self.cfg.get("engine.huesync.required", False)) and self.engine.name == "huesync":
            if not self.engine.state().connected:
                if now - self._required_log > 60:
                    log.warning("engine required but Hue Sync is unreachable - not launching ghost")
                    self._required_log = now
                return
        if m.item_id == self._eof_item:
            # the ghost already reached the end of this item; only relaunch if the
            # client is clearly not at the end any more (seeked back / restarted)
            if m.runtime_s is None or m.position_at(now) > m.runtime_s - 5.0:
                return
            self._eof_item = None
        if self._pause_stopped == m.item_id and m.paused:
            return                  # stopped because it sat paused; wait for play
        # the ghost about to launch decides the offset, not the one before it: a
        # first music ghost used to open on the film offset and seek a second later
        offset = self.offset_for(bool(spec is not None and spec.audio_only))
        target = m.position_at(now) + offset
        runtime = m.runtime_s
        if runtime and target >= runtime - 0.5:
            # the track is already over: a phone that finished a song sits at
            # its last position, and launching a ghost there sends mpv past the
            # end of the file, where it dies (rc=2) - over and over, burning
            # the launch budget. Wait for the client to report the next track.
            return
        self._launches = deque(t for t in self._launches if now - t < 300)
        same_item = m.item_id == self._last_launch_item
        cooldown_ok = (not same_item) or (now - self._last_launch >= float(g.get("relaunch_cooldown_s", 5.0)))
        if len(self._launches) >= int(g.get("relaunch_max_per_5min", 6)):
            if not self._guard_logged:
                log.error("launch rate guard hit (%d in 5 min) - not relaunching; check mpv_ghost.err",
                          int(g.get("relaunch_max_per_5min", 6)))
                self._guard_logged = True
            return
        if not cooldown_ok:
            return
        held = m.paused or m.frozen(now)
        start = target + (0.0 if held else self._startup_latency)
        if runtime:
            start = min(start, max(0.0, runtime - 1.0))   # never start past the end
        if spec is None or not spec.audio_only:
            self._prepare_display()      # music has no picture: no display to wake
        else:
            adev = spec.audio_device or ""
            if adev and not audio_endpoint_present(adev):
                # HDMI/DP audio lives on its display: asleep, the endpoint is
                # gone, and mpv would die rc=2 opening the file. Wake the
                # display (that brings the endpoint back) and retry next poll.
                # The wake is input (a mouse jiggle): not on every poll.
                if self._adev_warned != m.item_id:
                    self._adev_warned = m.item_id
                    log.warning("the ghost's audio output %s is not present - waking the "
                                "display; launching when it is back (if it never comes "
                                "back, pick it again in Display > Ghost audio output)", adev)
                if now - self._adev_woke >= 10.0:
                    self._adev_woke = now
                    wake_display()
                return
        log.info("'%s' playing on %s -> ghost at %.1fs (offset %+.2fs, startup est %.2fs)",
                 _asc(m.name), _asc(obs.report.device_label if obs.report else "?"), start,
                 offset, self._startup_latency)
        extra = {"audio_only": True, "audio_device": spec.audio_device} if spec.audio_only else {}
        self._ghost_audio_only = bool(spec.audio_only)
        self._music_gap = False
        try:
            self.ghost = GhostPlayer.launch(self.cfg, spec.url, spec.http_header, start,
                                            m.item_id, err_path=self._mpv_err, **extra)
        except Exception as e:
            log.error("ghost launch failed: %s", _asc(str(e)))
            self._last_launch = now
            self._launches.append(now)
            return
        self._last_launch = now
        self._last_launch_item = m.item_id
        self._ghost_binding = (act.binding or {}).get("id") or None
        self._launches.append(now)
        self._launch_mono = now
        self._guard_logged = False
        self._music_settled_at = now + MUSIC_SETTLE_S
        self._engine_started = False
        self._pending_seek = False
        self._standby = None
        self.state = GHOSTING
        if held:
            self.ghost.apply([Pause()], now)

    def _music_track_ended(self, now: float, g) -> None:
        """The music ghost played its file to the end and sits idle (mpv runs
        with --idle), waiting for the client's next track.

        It used to exit here, and the next song paid for a whole new mpv - a
        second of silence, so a second of dark lights, between every two songs.
        Now the next track is only a ``loadfile`` into the running player. If
        nothing follows within the music gap (a client that stays on the
        finished song), the ghost is closed like any other that ran out."""
        if self._eof_item != g.item_id:
            self._eof_item = g.item_id
            self._ended_since = now
            log.info("track ended -> the music ghost waits for the next one")
        self.state = SYNCING if self._engine_started else GHOSTING
        self._paused_since = None
        self._music_gap = True
        if self._ended_since is not None and now - self._ended_since >= self.music_gap \
                and self._idle_since is None:
            self._stop_ghost("the track ended and nothing followed")

    def _prepare_display(self) -> None:
        """The displays may have been switched off by Windows' idle timeout (the
        virtual ghost display goes with them) - wake them before mpv draws its
        first frame, or Hue Sync captures a dead screen until someone touches
        the mouse. A locked PC cannot be captured at all: say so once.

        Not while screen care has displays blacked out: they are on (held awake,
        showing black), and the wake-up jiggle is input - it would take the
        black away at every new episode of a binge."""
        if self.keep_awake_mode() != "off" and not self.care.covers and wake_display():
            log.info("waking the display so Hue Sync can capture the ghost")
        if desktop_locked():
            if not self._locked_warned:
                log.warning("this PC is locked - Hue Sync cannot capture the lock screen; "
                            "sign in and the colours will follow")
                self._locked_warned = True
        else:
            self._locked_warned = False

    # -- lockstep tick ------------------------------------------------------------
    def _tick(self, now: float) -> None:
        g = self.ghost
        if g is None:
            return
        if self._ghost_audio_only and g.alive() and getattr(g, "ended", False) \
                and g.end_reason == "eof" and not g.user_quit:
            self._music_track_ended(now, g)
            return
        if g.alive() and self._ghost_audio_only and getattr(g, "ended", False):
            g.kill()                    # an idle music ghost whose file failed: treat it as gone
        if not g.alive():
            rc = g.proc.poll()
            reason = g.end_reason or "?"
            log.info("ghost mpv exited (rc=%s, reason=%s)", rc, reason)
            g.kill()
            self.ghost = None
            if reason == "eof":
                self._eof_item = g.item_id
            self._refund_launch(g, now)
            if g.user_quit:
                self.enabled = False
                log.info("ghost closed by user -> sync disabled (hue-ghost on / tray to re-enable)")
            act = self.last_act
            if self._ghost_audio_only and not g.user_quit                     and (bool(getattr(act, "playing", False)) or reason == "eof"):
                # Music: the lights ride on the audio endpoint, not on a
                # picture, so there is nothing to stop for. Keep the engine
                # through the gap; the next track relaunches the ghost without
                # a stop/start blink, and if nothing follows the idle path
                # stops the lights after the music gap allowance. A track that
                # ran out counts even when the client already says "stopped":
                # CAMusic closes its session between songs, often in the very
                # poll the ghost reaches the end of the file.
                self.state = SYNCING if self._engine_started else GHOSTING
                self._paused_since = None
                self._standby = None
                self._idle_since = None
                self._music_gap = True
                return
            self.state = IDLE
            self.engine.stop()
            self._paused_since = None
            self._standby = None
            self._idle_since = None
            return

        obs = self.last_obs
        m = obs.model if obs else None
        if self._standby is not None:
            self.state = STANDBY
        if m is None:
            return

        if g.has_position and not self._engine_started and self._standby is None:
            lat = now - self._launch_mono
            self._startup_latency = 0.7 * self._startup_latency + 0.3 * min(lat, 5.0)
            log.info("ghost rendering (startup %.2fs) -> engine start", lat)
            self.engine.start()
            self._engine_started = True

        est = self.engine.state()
        if self._standby is not None:
            self.state = STANDBY
        else:
            self.state = SYNCING if (self._engine_started and est.syncing) else GHOSTING

        if pause_stop_due(self._paused_since, now, self._pause_stop_limit_s(m),
                          self._standby is not None) and self._engine_started:
            held = now - self._paused_since
            if m.media_type == "music":
                # Music does not wait on standby. A phone leaves its session open
                # for hours after the last track, so a parked music ghost is a
                # process doing nothing and a source that looks busy for ever.
                log.info("music paused for %.0f s -> sync off (nothing kept on standby)", held)
                self._pause_stopped = m.item_id
                if self.ghost is not None:
                    self._stop_ghost("music paused")
                    return
                self.engine.stop()      # the ghost is already gone (e.g. eof):
                self._engine_started = False
                self._plan_applied = None
                self._standby = None
                self._paused_since = None
                self.state = IDLE
                return
            log.info("client paused for %s -> lights off (ghost holds, resumes on play)",
                     "%.0f s" % held if held < 120 else "%.0f min" % (held / 60.0))
            self._lights_off("paused", now)

        target = m.position_at(now) + self.offset
        target_held = m.paused or m.frozen(now)     # client paused, or on a still frame after a seek
        gobs = GhostObs(pos=g.pos, paused=g.paused, buffering=(g.buffering or g.stalled),
                        speed=g.speed, last_seek_mono=g.last_seek_mono)
        if self._ghost_audio_only and getattr(g, "core_idle", False) and not g.paused:
            # a song just loaded reports its position before it plays (live: 0.5 s
            # before): a seek spent then is lost, and its cooldown holds the real
            # correction back. Loading is buffering: wait until it really plays.
            gobs.buffering = True
        force = self._pending_seek and g.has_position and not gobs.buffering
        if self._ghost_audio_only:
            self._learn_music_latency(now, g, gobs, target, target_held)
        actions = decide(target, target_held, gobs, self._lock_params(m), now, force_seek=force)
        if force:
            self._pending_seek = False
        if self._ghost_audio_only and actions and not target_held:
            # a music seek lands this much short of its aim (learned): aim past it.
            # Not while held: a paused ghost lands exactly where it is sent, and
            # aiming past the target there made it seek again on every tick.
            actions = [Seek(a.pos + self._seek_lead) if isinstance(a, Seek) else a for a in actions]
            # judged only in steady play: a seek in the first seconds of a file
            # waits on the network (little is cached yet), and teaches nothing
            if any(isinstance(a, Seek) for a in actions) and gobs.pos is not None \
                    and abs(gobs.pos - target) < 0.5 and now >= self._music_settled_at:
                self._seek_check = now + MUSIC_SEEK_CHECK_S
        if actions:
            g.apply(actions, now)
            for a in actions:
                if isinstance(a, Seek):
                    if gobs.pos is None:
                        log.info("initial seek to %.1fs", a.pos)
                    else:
                        log.info("drift %+.2fs -> seek to %.1fs%s", gobs.pos - target, a.pos,
                                 " (client event)" if force else "")
                    self.stats.seeks += 1
                elif isinstance(a, Pause):
                    log.info("%s -> ghost holds at %.1fs", "followed client paused" if m.paused
                             else "client buffering after seek", target)
                elif isinstance(a, Resume):
                    log.info("client playing again -> ghost resumes at %.1fs", target)
        # measure drift only in steady state: not held/buffering, and not in the
        # second after a seek (the jump we just commanded is not "drift")
        if gobs.pos is not None and not target_held and not gobs.buffering and not gobs.paused \
                and now - g.last_seek_mono > 1.0:
            self.stats.add(gobs.pos - target, abs(g.speed - 1.0) > 1e-6, m.locked)
        self._maybe_lock(now, m, g, gobs, target, target_held, actions)
        if self.stats.due(now):
            s = self.stats.flush()
            if s:
                log.info("sync quality (last %ds): mean|drift| %.3fs p95 %.3fs max %.3fs bias %+.3fs "
                         "seeks %d nudging %d/%d ticks%s", int(self.stats.period), s["mean_abs"],
                         s["p95_abs"], s["max_abs"], s["mean"], s["seeks"], s["nudge_ticks"], s["n"],
                         " locked %d/%d" % (s["locked_ticks"], s["n"]) if self.params.time_lock else "")

    def _learn_music_latency(self, now: float, g, gobs: GhostObs, target: float, held: bool) -> None:
        """Where a music swap or seek really landed, against where it was
        aimed: both are learned, so the next one lands on the beat."""
        steady = gobs.pos is not None and not held and not gobs.buffering and not gobs.paused
        if self._swap_opened is not None and (steady or now - self._swap_opened[0] >= 5.0):
            # the first moment the new song really plays (loading counts as buffering)
            opened = self._swap_opened
            self._swap_opened = None
            if steady and now - opened[0] < 5.0:
                miss = gobs.pos - target        # < 0: it plays behind - the loadfile took longer
                if abs(miss) < 2.0:
                    self._swap_latency = round(min(max(
                        0.7 * self._swap_latency + 0.3 * (self._swap_latency - miss), 0.0), 3.0), 3)
                    self._music_learned = True
        if self._seek_check is not None and now >= self._seek_check:
            self._seek_check = None
            if steady and now - g.last_seek_mono < 2 * MUSIC_SEEK_CHECK_S:
                miss = gobs.pos - target
                if abs(miss) < 0.2:
                    self._seek_lead = round(min(max(self._seek_lead - 0.3 * miss, 0.0), 0.3), 3)
                    self._music_learned = True

    def _lock_params(self, m) -> Params:
        """The lockstep parameters for this tick. With the time lock armed but
        not yet engaged the deadband narrows inside the lock window, so the
        ghost converges to ~0 instead of stopping at the deadband's edge.

        Music never touches the tempo: exact seeks beyond ``sync.music_seek_s``."""
        if self._ghost_audio_only:
            if m.locked and not self.params.time_lock:
                m.unlock()
            band = max(0.01, float(self.cfg.get("sync.music_seek_s", 0.04) or 0.04))
            return dataclasses.replace(self.params, speed_nudge=False, deadband_s=band,
                                       seek_threshold_s=band, seek_cooldown_s=MUSIC_SEEK_COOLDOWN_S)
        if not self.params.time_lock:
            if m.locked:
                m.unlock()
                log.info("time lock switched off -> back to per-report corrections")
            return self.params
        if m.locked:
            m.lock_release_s = self.params.time_lock_release_s   # a changed setting applies at once
            return self.params
        return dataclasses.replace(self.params, deadband_s=min(self.params.deadband_s, LOCK_WINDOW_S * 0.75))

    def _maybe_lock(self, now: float, m, g, gobs: GhostObs, target: float, target_held: bool,
                    actions: list) -> None:
        """Engage the time lock: the ghost sits on the target, playing at 1.0x,
        and the client's position has been steady for a few reports."""
        if not self.params.time_lock or m.locked or target_held:
            return
        # Music holds a lock for the whole song (watcher._hold_music_lock), so it
        # is only taken on a good estimate: a noisy player (Jellyfin for Android,
        # +/-0.14 s per timed report) gets a few more reports to agree first.
        settle = LOCK_SETTLE_REPORTS
        if m.media_type == "music" and m.jitter >= MUSIC_PRECISE_JITTER_S:
            settle = MUSIC_NOISY_SETTLE_REPORTS
        if not m.settled(settle, LOCK_AGREE_S) or self._pending_seek or actions:
            return
        if gobs.pos is None or gobs.buffering or gobs.paused or now - g.last_seek_mono < 2.0:
            return
        drift = gobs.pos - target
        window = LOCK_WINDOW_S
        if self._ghost_audio_only:      # music corrects by seeks: it sits within their band
            window = max(window, float(self.cfg.get("sync.music_seek_s", 0.04) or 0.04))
        if abs(g.speed - 1.0) > 0.005 or abs(drift) > window:
            return
        m.lock(self.params.time_lock_release_s)
        log.info("time lock engaged at drift %+.3fs ('%s' at %.1fs)", drift, _asc(m.name), m.position_at(now))

    def _stop_ghost(self, reason: str) -> None:
        g = self.ghost
        if g is None:
            return
        log.info("stopping ghost (%s)", reason)
        self._refund_launch(g, time.monotonic())
        self.engine.stop()
        if self._engine_started:
            self.engine.wait_until(lambda s: not s.syncing or not s.connected, ENGINE_STOP_WAIT_S)
        g.kill()
        self.ghost = None
        self.state = IDLE
        self._engine_started = False
        self._ghost_binding = None
        self._parked_since = None
        self._idle_since = None
        self._paused_since = None
        self._standby = None
        self._plan_applied = None       # the next session re-asserts the binding's settings
        s = self.stats.flush()
        if s:
            log.info("sync quality (session tail): mean|drift| %.3fs p95 %.3fs max %.3fs seeks %d",
                     s["mean_abs"], s["p95_abs"], s["max_abs"], s["seeks"])

    # -- control surface ----------------------------------------------------------
    def _binding_status(self) -> list[dict]:
        """Every binding, whether it is on, and which one is driving the lights.
        Home Assistant turns this into a switch each."""
        act = self.last_act
        live = (act.binding or {}).get("id") if act.playing else None
        out = []
        for b in self.cfg.bindings():
            probs = self.cfg.binding_problems(b)
            out.append({
                "id": b["id"],
                "name": b["name"] or b["id"],
                "source": b["source"],
                "enabled": bool(b["enabled"]),
                "active": b["id"] == live,
                "area_id": b["area_id"] or None,
                "area_name": b["area_name"] or None,
                "mode": b["mode"] or None,
                "intensity": b["intensity"] or None,
                "use_audio": b.get("use_audio"),
                "exe": b["exe"] or None,
                "device": b["device_id"] or b["device_name_contains"] or None,
                "problems": probs,
            })
        return out

    def set_binding_enabled(self, key: str, on: bool) -> dict:
        """Turn one binding on or off by id (or by name, for convenience)."""
        binds = self.cfg.bindings()
        want = str(key or "").strip().lower()
        hit = next((b for b in binds if b["id"].lower() == want), None) or \
            next((b for b in binds if (b["name"] or "").lower() == want), None)
        if hit is None:
            raise ValueError("no binding %r (have: %s)" % (key, ", ".join(b["id"] for b in binds)))
        if bool(hit["enabled"]) != bool(on):
            hit["enabled"] = bool(on)
            self.cfg.set_bindings(binds)
            if self.cfg.path:
                try:
                    self.cfg.save()
                except OSError as e:
                    log.warning("could not persist bindings: %s", e)
            if self.ghost is not None and not on and \
                    (self.last_act.binding or {}).get("id") == hit["id"]:
                self._stop_ghost("binding switched off")
            self._rebuild_sources()
            self.last_act = IDLE_ACTIVITY
            self._plan_applied = None
            log.info("binding '%s' %s", hit["name"] or hit["id"], "on" if on else "off")
        return {"id": hit["id"], "enabled": bool(on)}

    def _find_binding(self, binds: list[dict], key: str) -> dict:
        want = str(key or "").strip().lower()
        hit = next((b for b in binds if b["id"].lower() == want), None) or \
            next((b for b in binds if (b["name"] or "").lower() == want), None)
        if hit is None:
            raise ValueError("no binding %r (have: %s)" % (key, ", ".join(b["id"] for b in binds)))
        return hit

    def set_binding_fields(self, key: str, fields: dict) -> dict:
        """Change one source's entertainment area, mode or intensity (Home
        Assistant, per source). "" hands the choice back: the area Hue Sync is
        on, the global mode or intensity. The playing source's plan changes
        with it, so the lights follow at once."""
        binds = self.cfg.bindings()
        hit = self._find_binding(binds, key)
        if "area_id" in fields:
            aid = str(fields["area_id"] or "")
            if aid:
                area = next((a for a in self.areas() if aid in (a["id"], a["name"])), None)
                if area is None:
                    raise ValueError("no entertainment area %r (have: %s)"
                                     % (aid, ", ".join(a["name"] for a in self.areas())))
                hit["area_id"], hit["area_name"] = area["id"], area["name"]
            else:
                hit["area_id"], hit["area_name"] = "", ""
        for field, allowed in (("mode", MODES), ("intensity", INTENSITIES)):
            if field in fields:
                v = str(fields[field] or "").lower()
                if v and v not in allowed:
                    raise ValueError("%s must be one of %s, or empty" % (field, ", ".join(allowed)))
                hit[field] = v
        if "use_audio" in fields:
            hit["use_audio"] = _tri_state(fields["use_audio"])
        self.cfg.set_bindings(binds)
        if self.cfg.path:
            try:
                self.cfg.save()
            except OSError as e:
                log.warning("could not persist bindings: %s", e)
        self._rebuild_sources()
        log.info("binding '%s': %s", _asc(hit["name"] or hit["id"]),
                 ", ".join("%s %s" % (k, hit.get(k) or "(default)") for k in fields))
        return {"id": hit["id"], **{k: hit.get(k) for k in fields}}

    def set_pin(self, key) -> dict:
        """Pick the source to sync now (Home Assistant's "Sync source"): it goes
        first whenever it plays, ahead of the list order, and the others still
        sync while it does not play. None or "" = the list order."""
        pin = ""
        if key:
            hit = self._find_binding(self.cfg.bindings(), str(key))
            if not hit["enabled"]:
                self.set_binding_enabled(hit["id"], True)   # picking a source that is off turns it on
            pin = hit["id"]
        if (self.cfg.get("sync.pin") or "") != pin:
            self.cfg.set("sync.pin", pin)
            if self.cfg.path:
                try:
                    self.cfg.save()
                except OSError as e:
                    log.warning("could not persist the source pick: %s", e)
            self._rebuild_sources()
            log.info("sync source: %s", ("'%s' first whenever it plays" % pin) if pin else "list order")
        return {"pin": pin or None}

    def areas(self) -> list[dict]:
        """The entertainment areas Hue Sync knows, re-read at most every 30 s
        (it is a file read, and /status is polled every few seconds)."""
        now = time.monotonic()
        if self._areas_cache is None or now - self._areas_read >= 30.0:
            try:
                self._areas_cache = [{"id": a["id"], "name": a["name"]} for a in (self.engine.areas() or [])]
            except Exception:
                self._areas_cache = self._areas_cache or []
            self._areas_read = now
        return list(self._areas_cache)

    def settings(self) -> dict:
        """Every tunable a remote control (Home Assistant) shows, as it is now."""
        g = self.cfg.section("ghost")
        return {
            "offset_s": float(self.cfg.get("sync.offset_s", 0.0)),
            "music_offset_s": self.cfg.get("sync.music_offset_s"),
            "lights_off_delay_s": self.lights_off_delay,
            "idle_stop_delay_s": float(self.cfg.get("sync.idle_stop_delay_s", 10.0)),
            "music_gap_s": float(self.cfg.get("sync.music_gap_s", 10.0) or 0.0),
            "music_pause_stop_s": float(self.cfg.get("sync.music_pause_stop_s", 15.0) or 0.0),
            "pause_stop_min": float(self.cfg.get("sync.pause_stop_min", 0.0) or 0.0),
            "time_lock": bool(self.params.time_lock),
            "keep_awake": self.keep_awake_mode(),
            "pixel_shift_min": float(g.get("pixel_shift_min", 0.0) or 0.0),
            "blackout_idle_min": float(g.get("blackout_idle_min", 0.0) or 0.0),
            "manage_area": bool(self.cfg.get("engine.huesync.manage_area", True)),
        }

    def status(self) -> dict:
        with self._lock:
            obs = self.last_obs
            act = self.last_act
            m = obs.model if obs else None
            now = time.monotonic()
            g = self.ghost
            est = self.engine.state()
            f = self.cfg.get("jellyfin.follow", {}) or {}
            target = (m.position_at(now) + self.offset) if m else None
            drift = (g.pos - target) if (g and g.pos is not None and target is not None) else None
            return {
                "version": __version__,
                "enabled": self.enabled,
                "state": self.state,
                "config_path": self.cfg.path,
                "setup_required": self.cfg.problems(),
                "restart_required": list(self.restart_required),
                "ui_url": self.ui_url(),
                "jellyfin": {"url": self.cfg.get("jellyfin.url"), "ok": self.jf_ok, "error": self.jf_error,
                             "clock_offset_s": self.watcher.clock.value},
                "bindings": self._binding_status(),
                "source": {
                    "kind": act.kind,
                    "name": act.title or None,
                    "needs_ghost": act.needs_ghost,
                    "binding": (act.binding or {}).get("id") or None,
                },
                "follow": {
                    "device_id": f.get("device_id", ""),
                    "device_name_contains": f.get("device_name_contains", ""),
                    "device": (obs.report.device_label if obs and obs.report else None),
                    "seen": bool(obs.seen) if obs else False,
                    "playing": bool(obs.playing) if obs else False,
                    "item": m.name if m else None,
                    "item_id": m.item_id if m else None,
                    "position_s": round(m.position_at(now), 2) if m else None,
                    "runtime_s": m.runtime_s if m else None,
                    "paused": m.paused if m else None,
                    "paused_for_s": (round(now - self._paused_since, 1)
                                     if (m and m.paused and self._paused_since is not None) else None),
                    "buffering": m.frozen(now) if m else None,
                    "area_id": (obs.player.area_id if obs and obs.player else None),
                    "area_name": (obs.player.area_name if obs and obs.player else None),
                    "reports": m.reports if m else 0,
                    "stall_estimates": {k: round(v, 2) for k, v in self.watcher.stalls.est.items()},
                    # how this player's reports err (learned): see watcher.ReportProfile
                    "profile": (m.profile.to_dict() if (m and m.profile) else None),
                    "envelope_gap_s": (round(m.env_gap, 3)
                                       if (m and m.envelope and m.env_gap is not None) else None),
                },
                "music": {
                    "offset_s": self.cfg.get("sync.music_offset_s"),
                    "swap_latency_s": self._swap_latency,
                    "seek_lead_s": self._seek_lead,
                },
                "ghost": {
                    "alive": bool(g and g.alive()),
                    "position_s": round(g.pos, 2) if (g and g.pos is not None) else None,
                    "paused": g.paused if g else None,
                    "buffering": (g.buffering or g.stalled) if g else None,
                    "speed": g.speed if g else None,
                    "seeks": g.seeks if g else 0,
                    "nudges": g.nudges if g else 0,
                    "standby": self._standby,
                    # parked: alive, holding its frame, because another source
                    # has the lights - closed when the standby window is over
                    "parked": self._parked_since is not None,
                    "standby_closes_in_s": (round(max(0.0, float(self.cfg.get("sync.idle_stop_delay_s", 10.0))
                                                      - (now - self._idle_since)), 1)
                                            if (g and self._idle_since is not None) else None),
                },
                "system": {
                    "keep_awake": self.keep_awake_mode(),
                    "awake_held": self._awake == "display",
                    "pc_held_awake": bool(self._awake),
                    "locked": desktop_locked(),
                    "screen_care": self.care.status(),
                },
                "drift_s": round(drift, 3) if drift is not None else None,
                # while engaged the drift above is ~0 by design, so
                # residual_s is the honest number - how far the client's own
                # reports sit from the locked timeline
                "time_lock": {
                    "enabled": self.params.time_lock,
                    "engaged": bool(m is not None and m.locked),
                    "residual_s": (round(m.lock_residual, 3)
                                   if (m is not None and m.locked and m.lock_residual is not None) else None),
                },
                "drift_last_minute": self.stats.last_summary,
                "engine": est.as_dict(),
                "offset_s": self.offset,
                # What is live, not what the config defaults to. Since every
                # source carries its own mode and intensity, the global settings
                # are only the fallback - reporting them left every control that
                # shows "the mode" (Home, the tray, the CLI, Home Assistant) on
                # the previous session's choice for the whole of this one.
                "intensity": self._live("intensity", est.intensity, INTENSITIES),
                "mode": self._live("mode", est.mode, MODES),
                "intensity_default": self.cfg.get("engine.huesync.intensity"),
                "mode_default": self.cfg.get("engine.huesync.mode"),
                "use_audio": self.cfg.get("engine.huesync.use_audio"),
                "manage_area": bool(self.cfg.get("engine.huesync.manage_area", True)),
                "lights_off_delay_s": self.lights_off_delay,
                # for remote controls: the source picked to sync now, the areas
                # a source can be sent to, and every tunable as it stands
                "pin": self.cfg.get("sync.pin") or None,
                "areas": self.areas(),
                "settings": self.settings(),
            }

    def _live(self, key: str, value: str | None, allowed: tuple) -> str | None:
        """What the engine is really set to, falling back to the global default.

        The engine has nothing to report before it connects, and Hue Sync also
        has modes hue-ghost has no control for ("scenes"), so a value that is
        not one of ours is not one to show."""
        return value if value in allowed else self.cfg.get("engine.huesync." + key)

    def ui_url(self) -> str:
        c = self.cfg.section("control")
        return "http://127.0.0.1:%d/" % int(c.get("port") or 8787)

    def apply_config(self, partial: dict) -> dict:
        """Merge a partial nested config in, save it and apply it live where
        possible. Returns what still needs a restart."""
        with self._lock:
            old = copy.deepcopy(self.cfg.data)
            self.cfg.data = _deep_merge(self.cfg.data, partial)
            new = self.cfg.data
            problems = self.cfg.problems()
            if self.cfg.path:
                self.cfg.save()

            def changed(*keys):
                return any(self.cfg.get(k) != _get(old, k) for k in keys)

            server_moved = changed("jellyfin.url", "jellyfin.api_key")
            if server_moved:
                self.jf_ok = False
            if server_moved or changed("jellyfin.poll_interval_s", "pc") \
                    or self.cfg.bindings() != self._bindings_snapshot:
                if self.ghost is not None:
                    self._stop_ghost("followed client changed")
                self._rebuild_sources(keep_client=not server_moved)
                self.last_obs = None
                self.last_act = IDLE_ACTIVITY
                self._plan_applied = None
            self.watcher.jitter_tol = float(self.cfg.get("sync.jitter_tolerance_s", 1.5))
            self.watcher.precise_timing = bool(self.cfg.get("sync.time_lock", True))
            self.params = Params.from_config(self.cfg)
            if changed("engine.type", "engine.huesync.host", "engine.huesync.port",
                       "engine.huesync.launch_exe", "engine.httphook.url"):
                try:
                    self.engine.stop()
                    self.engine.close()
                except Exception:
                    pass
                self.engine = build_engine(self.cfg, on_app_change=self._engine_events.put)
                self._engine_started = False
                self._plan_applied = None
                self.state = GHOSTING if self.ghost is not None else IDLE
            self._push_engine_prefs()
            if changed("enabled"):
                self.set_enabled(bool(self.cfg.get("enabled", True)))
            if changed("log_level"):
                lvl = getattr(logging, str(self.cfg.get("log_level", "INFO")).upper(), logging.INFO)
                logging.getLogger("hue-ghost").setLevel(lvl)
            if changed("control.bind", "control.port", "control.token"):
                self.restart_required = sorted(set(self.restart_required) | {"control"})
            log.info("config updated%s", (" (restart needed: %s)" % ", ".join(self.restart_required))
                     if self.restart_required else "")
            return {"ok": True, "problems": problems, "restart_required": list(self.restart_required)}

    def test_ghost(self, screen_name: str | None = None, seconds: int = 8) -> dict:
        """Show a colour test pattern on the ghost display (no media needed)."""
        with self._lock:
            if self.ghost is not None:
                raise ValueError("a ghost is playing right now; stop playback first")
            if self._test_proc is not None and self._test_proc.poll() is None:
                raise ValueError("a test is already running")
            cfg = Config(copy.deepcopy(self.cfg.data), self.cfg.path)
            if screen_name:
                cfg.set("ghost.screen_name", screen_name)
                cfg.set("ghost.screen_index", None)
            seconds = max(2, min(int(seconds), 60))
            args = mpv_args(cfg, "hue-ghost test pattern")
            args = [a for a in args if not a.startswith("--input-ipc-server=")]
            args += ["--length=%d" % seconds,
                     "av://lavfi:testsrc2=size=1280x720:rate=30:duration=%d" % seconds]
            self._test_proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                               stdin=subprocess.DEVNULL)
            log.info("test pattern on %s for %ds", cfg.get("ghost.screen_name") or "current display", seconds)
            return {"ok": True, "seconds": seconds}

    def action(self, name: str, payload: dict) -> dict:
        with self._lock:
            if name == "on":
                self.set_enabled(True)
            elif name == "off":
                self.set_enabled(False)
            elif name == "toggle":
                self.set_enabled(not self.enabled)
            elif name == "reload":
                self.reload()
            elif name == "set":
                self._apply_settings(payload)
            else:
                raise ValueError("unknown action " + name)
            return {"ok": True, "enabled": self.enabled, "state": self.state,
                    "offset_s": self.offset, "intensity": self.cfg.get("engine.huesync.intensity")}

    def set_enabled(self, on: bool) -> None:
        if on == self.enabled:
            return
        self.enabled = on
        log.info("sync %s", "enabled" if on else "disabled")
        if not on and self.ghost is not None:
            self._stop_ghost("disabled")
        if not on:
            self.engine.stop()
            self._plan_applied = None
        self._guard_logged = False
        self._launches.clear()

    def reload(self) -> None:
        self.cfg.reload()
        self.params = Params.from_config(self.cfg)
        self.watcher.jitter_tol = float(self.cfg.get("sync.jitter_tolerance_s", 1.5))
        self.watcher.precise_timing = bool(self.cfg.get("sync.time_lock", True))
        self.watcher.poll_interval = float(self.cfg.get("jellyfin.poll_interval_s", 0.5))
        self._push_engine_prefs()
        log.info("config reloaded (offset %+.2fs, intensity %s)", self.offset,
                 self.cfg.get("engine.huesync.intensity"))

    def _push_engine_prefs(self) -> None:
        e = self.engine
        # While something plays, its Plan owns mode and intensity: a binding's
        # own values, music mode for a song. Pushing the global defaults here
        # used to flip a playing phone to video/high whenever any setting was
        # saved - even an offset nudge. A changed default still arrives: the
        # plan is rebuilt from the config every poll and re-applied on change.
        if not self.last_act.playing:
            for field, setter in (("intensity", e.set_intensity), ("mode", e.set_mode)):
                if self.cfg.get("engine.huesync." + field):
                    try:
                        setter(self.cfg.get("engine.huesync." + field))
                    except (ValueError, RuntimeError):
                        pass
        try:
            # the playing source's own "react to audio" outranks the default
            # (looked up afresh: the activity holds the binding as it was before the save)
            live = (self.last_act.binding or {}).get("id") if self.last_act.playing else None
            e.set_use_audio(self.cfg.binding_use_audio(
                next((b for b in self.cfg.bindings() if live and b["id"] == live), None)))
        except (ValueError, RuntimeError):
            pass
        for attr, key in (("set_manage_area", "manage_area"),
                          ("set_manage_monitor", "manage_monitor"),
                          ("set_manage_audio_device", "manage_audio_device")):
            if hasattr(e, attr):
                try:
                    getattr(e, attr)(bool(self.cfg.get("engine.huesync." + key, True)))
                except (ValueError, RuntimeError):
                    pass

    def _drain_engine_events(self) -> None:
        """Adopt what the user changed in the engine's own app.

        The engine has already stopped asserting its own choice, so the change
        is live either way; this decides where it is *kept*. Deliberately no
        ``_push_engine_prefs()``: the engine already holds the value, and
        pushing it back is precisely the feedback loop this is meant to avoid.

        What is saved is the **global default**, which is what sources that
        have not chosen a mode or an intensity of their own fall back to. So a
        source that *has* chosen one keeps its choice out of it: the engine
        holds what the user picked for the rest of this session, and the source
        re-asserts its own at the next. Saving it globally instead would let a
        mode picked during a film quietly become the setting for every other
        source - which is exactly how a config ends up defaulting to something
        nobody chose."""
        got: dict = {}
        while True:
            try:
                got.update(self._engine_events.get_nowait())
            except queue.Empty:
                break
        if not got:
            return
        saved, held = {}, []
        for field, allowed in (("mode", MODES), ("intensity", INTENSITIES)):
            v = got.get(field)
            if v not in allowed:
                continue
            if self._source_sets_its_own(field):
                held.append(field)
                continue
            if self.cfg.get("engine.huesync." + field) != v:
                self.cfg.set("engine.huesync." + field, v)
                saved[field] = v
        if held:
            log.info("%s changed in the Hue Sync app; kept for this session ('%s' sets its own)",
                     " and ".join(held),
                     _asc((self.last_act.binding or {}).get("name") or "the source playing"))
        if not saved:
            # e.g. Hue Sync's "scenes", which hue-ghost has no setting for: the
            # engine stops fighting it, but the saved config stays one we can run
            log.debug("Hue Sync is in %s; nothing new to store, leaving the config alone", got)
            return
        log.info("adopted from the Hue Sync app: %s", saved)
        if self.cfg.path:
            try:
                self.cfg.save()
            except OSError as e:
                log.warning("could not persist the adopted settings: %s", e)

    def _set_look(self, field: str, value: str) -> bool:
        """Mode or intensity picked on Home, the tray, the CLI or Home Assistant.

        It takes effect on the lights at once. It becomes the global default
        only when the source playing does not carry its own value - otherwise a
        choice made during the Apple TV film would silently become the default
        for every other source. Returns whether the config changed."""
        if self.last_act.playing:
            try:
                (self.engine.set_mode if field == "mode" else self.engine.set_intensity)(value)
            except (ValueError, RuntimeError):
                pass
            if self._source_sets_its_own(field):
                log.info("%s -> %s for this session ('%s' sets its own)", field, value,
                         _asc((self.last_act.binding or {}).get("name") or "the source playing"))
                return False
        self.cfg.set("engine.huesync." + field, value)
        return True

    def _source_sets_its_own(self, field: str) -> bool:
        """Whether the binding currently driving the lights carries its own
        value for ``mode`` or ``intensity``."""
        act = self.last_act
        b = act.binding if act.playing else None
        return bool(b and (b.get(field) or ""))

    def _pause_stop_limit_s(self, m) -> float:
        """How long the followed client may sit paused before the lights go out.

        Music gets its own, much shorter limit. A film is paused to be come back
        to, so minutes are right; a phone that stops a song usually just leaves
        the session open in the background, and waiting for that to disappear
        means the lights stay on long after the music has ended."""
        if m is not None and getattr(m, "media_type", "video") == "music":
            return float(self.cfg.get("sync.music_pause_stop_s", 15.0) or 0.0)
        return float(self.cfg.get("sync.pause_stop_min", 0.0) or 0.0) * 60.0

    def _apply_settings(self, p: dict) -> None:
        changed = False
        if "enabled" in p:
            self.set_enabled(bool(p["enabled"]))
        if "intensity" in p:
            lvl = str(p["intensity"]).lower()
            if lvl not in INTENSITIES:
                raise ValueError("intensity must be one of " + ", ".join(INTENSITIES))
            changed = self._set_look("intensity", lvl) or changed
        if "mode" in p:
            mode = str(p["mode"]).lower()
            if mode not in MODES:
                raise ValueError("mode must be one of " + ", ".join(MODES))
            changed = self._set_look("mode", mode) or changed
        if "use_audio" in p:
            self.cfg.set("engine.huesync.use_audio", _tri_state(p["use_audio"]))
            changed = True
        if "manage_area" in p:
            self.cfg.set("engine.huesync.manage_area", bool(p["manage_area"]))
            changed = True
        if "pause_stop_min" in p:
            self.cfg.set("sync.pause_stop_min", max(0.0, round(float(p["pause_stop_min"]), 2)))
            changed = True
        if "music_pause_stop_s" in p:
            self.cfg.set("sync.music_pause_stop_s", max(0.0, round(float(p["music_pause_stop_s"]), 1)))
            changed = True
        if "lights_off_delay_s" in p:
            self.cfg.set("sync.lights_off_delay_s", max(0.0, round(float(p["lights_off_delay_s"]), 2)))
            changed = True
        if "keep_awake" in p:
            mode = str(p["keep_awake"]).lower()
            if mode not in KEEP_AWAKE_MODES:
                raise ValueError("keep_awake must be one of " + ", ".join(KEEP_AWAKE_MODES))
            self.cfg.set("ghost.keep_awake", mode)
            changed = True
        for key in ("pixel_shift_min", "blackout_idle_min"):     # screen care; 0 = off
            if key in p:
                self.cfg.set("ghost." + key, max(0.0, round(float(p[key]), 1)))
                changed = True
        if "time_lock" in p:
            self.cfg.set("sync.time_lock", bool(p["time_lock"]))
            self.params = Params.from_config(self.cfg)
            self.watcher.precise_timing = self.params.time_lock
            changed = True
        if "offset_s" in p or "offset_delta" in p:
            # the FILM offset. Not self.offset: that is the one in use, the
            # music offset while music plays - a nudge there moved the film
            # offset to the music value plus the nudge.
            film = float(self.cfg.get("sync.offset_s", 0.0))
            new = float(p["offset_s"]) if "offset_s" in p else film + float(p["offset_delta"])
            if self.cfg.get("sync.music_offset_s") is None and "music_offset_s" not in p:
                # music shared the film offset: it keeps the value it had, so
                # tuning films never moves the music
                self.cfg.set("sync.music_offset_s", round(film, 3))
            self.cfg.set("sync.offset_s", round(new, 3))
            changed = True
        if "brightness_step" in p:
            self.engine.adjust_brightness(int(p["brightness_step"]))
        if "brightness" in p:
            level = int(p["brightness"])
            if not 0 <= level <= 100:
                raise ValueError("brightness must be 0-100")
            self.engine.set_brightness(level)
        if "music_offset_s" in p:
            v = p["music_offset_s"]
            self.cfg.set("sync.music_offset_s", None if v is None or v == "" else round(float(v), 3))
            changed = True
        for key, lo, hi in (("music_gap_s", 0.0, 600.0), ("idle_stop_delay_s", 0.0, 3600.0)):
            if key in p:
                self.cfg.set("sync." + key, min(hi, max(lo, round(float(p[key]), 1))))
                changed = True
        if "pin" in p:
            self.set_pin(p["pin"])
        if "binding" in p:
            b = p["binding"] or {}
            if not isinstance(b, dict) or "key" not in b:
                raise ValueError('binding must be {"key": "<id>", "enabled": true|false, '
                                 '"area_id": ..., "mode": ..., "intensity": ...}')
            fields = {k: b[k] for k in BINDING_FIELDS if k in b}
            if fields:
                self.set_binding_fields(str(b["key"]), fields)
            if "enabled" in b or not fields:
                self.set_binding_enabled(str(b["key"]), bool(b.get("enabled", True)))
        if changed:
            self._push_engine_prefs()
            if self.cfg.path:
                try:
                    self.cfg.save()
                except OSError as e:
                    log.warning("could not persist settings: %s", e)
            mo = self.cfg.get("sync.music_offset_s")
            log.info("settings: film offset %+.2fs, music offset %s, intensity %s",
                     float(self.cfg.get("sync.offset_s", 0.0)),
                     "%+.2fs" % mo if isinstance(mo, (int, float)) else "(film)",
                     self.cfg.get("engine.huesync.intensity"))
