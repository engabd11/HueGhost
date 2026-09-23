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
from .sources import IDLE as IDLE_ACTIVITY, build_sources
from .watcher import Observation, PlayerSet, SessionWatcher
from .winutil import desktop_locked, keep_awake, wake_display

log = logging.getLogger("hue-ghost")

IDLE, GHOSTING, SYNCING, STANDBY = "idle", "ghosting", "syncing", "standby"
TICK_S = 0.25
ENGINE_STOP_WAIT_S = 2.0


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


def pause_stop_due(paused_since: float | None, now: float, limit_min: float,
                   already_stopped: bool) -> bool:
    """True when sync should stop because the followed client has been paused
    for ``limit_min`` minutes (0 disables the feature)."""
    if already_stopped or paused_since is None or limit_min <= 0:
        return False
    return now - paused_since >= limit_min * 60.0


class DriftStats:
    """Per-minute summary of ghost-vs-target drift, so sync quality is measured."""

    def __init__(self, period_s: float = 60.0):
        self.period = period_s
        self.samples: list[float] = []
        self.seeks = 0
        self.nudge_ticks = 0
        self.started = time.monotonic()
        self.current: float | None = None
        self.last_summary: dict | None = None

    def add(self, drift: float, nudging: bool) -> None:
        self.current = drift
        self.samples.append(drift)
        if nudging:
            self.nudge_ticks += 1

    def due(self, now: float) -> bool:
        return now - self.started >= self.period

    def flush(self) -> dict | None:
        if not self.samples:
            self.started = time.monotonic()
            self.seeks = 0
            self.nudge_ticks = 0
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
        }
        self.last_summary = summary
        self.samples = []
        self.seeks = 0
        self.nudge_ticks = 0
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
        self._awake = False           # displays currently held awake by us
        self._locked_warned = False
        self._launches: deque[float] = deque()
        self._last_launch = 0.0
        self._last_launch_item: str | None = None
        self._guard_logged = False
        self._pending_seek = False
        self._engine_started = False
        self._eof_item: str | None = None
        self._last_report_count = 0
        self._stalls_saved_mono = float("-inf")
        self._startup_latency = 1.0   # EMA of launch -> first time-pos
        self._launch_mono = 0.0
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
        old = self._jelly
        self.sources.close()
        self.sources = build_sources(self.cfg,
                                     jf_client=(old.client if (old and keep_client) else None))
        self._bindings_snapshot = self.cfg.bindings()
        s = self._jelly
        if s is not None and stalls is not None:
            s.watcher.stalls = stalls

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
            log.warning("control API unavailable on port %s: %s", port, e)
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
                    next_poll = now + float(self.cfg.get("jellyfin.poll_interval_s", 0.5))
                with self._lock:
                    self._tick(time.monotonic())
                    self._sync_power()
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
        # a music ghost has no picture, and a PC source is on a screen the user
        # is already looking at: neither needs a display held awake for us
        playing_on_a_display = (self.ghost is not None and not self._ghost_audio_only) \
            or (self.ghost is None and self._engine_started)
        want = mode == "always" or (mode == "playing" and playing_on_a_display)
        if want == self._awake:
            return
        if keep_awake(want):
            log.info("displays held awake %s" % ("(always)" if mode == "always" else "while the ghost plays")
                     if want else "displays may sleep again")
        self._awake = want

    def stop(self) -> None:
        self._stop.set()

    def request_restart(self) -> None:
        """Stop the loop; the front end (cli/tray) re-launches the process."""
        self.restart_requested = True
        log.info("restart requested from the UI")
        threading.Timer(0.5, self._stop.set).start()

    def _shutdown(self) -> None:
        with self._lock:
            if self.ghost is not None:
                self._stop_ghost("daemon exit")
            try:
                self.engine.stop()
                self.engine.wait_until(lambda s: not s.syncing or not s.connected, ENGINE_STOP_WAIT_S)
            finally:
                self.engine.close()
            if self._awake:
                keep_awake(False)
                self._awake = False
        if self.control:
            self.control.stop()
        log.info("stopped")

    @property
    def offset(self) -> float:
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
            pc.ignore_pid(self.ghost.proc.pid if (self.ghost and self.ghost.proc) else None)
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
            if act.playing:
                self.engine.apply_plan(act.plan)

        if obs is not None and obs.model is not None and obs.model.last_debug \
                and log.isEnabledFor(logging.DEBUG):
            if obs.model.reports != self._last_report_count or act.event == "new_item":
                log.debug("%s", obs.model.last_debug)
                obs.model.last_debug = ""
            self._last_report_count = obs.model.reports
        self._persist_stalls(now)

        if act.playing and self.enabled:
            self._playing(now, act)
        else:
            self._idle(now, act)

    def _playing(self, now: float, act) -> None:
        """Something is playing. What that costs us depends on where it is: a
        client on the network has to be mirrored, an app on this PC does not."""
        if act.event == "new_item":
            self.engine.apply_plan(act.plan)
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
        if act.event == "new_item" and obs.stale:
            log.warning("first report for '%s' is stale (>30 s old); position may be off", _asc(m.name))
        if self.ghost is not None and self.ghost.item_id != m.item_id:
            log.info("followed client switched to '%s' -> relaunching ghost", _asc(m.name))
            self._stop_ghost("item changed")
        if self.ghost is None:
            self._maybe_launch(now, act)
        elif act.event in ("seek", "resume", "new_item", "resync"):
            self._pending_seek = True
            if act.event == "seek":
                log.info("followed client seeked to %.1fs (holding %.1fs for buffering)",
                         m.anchor_pos, max(0.0, m.anchor_mono - now))
            elif act.event == "resync":
                log.info("client resumed earlier/later than predicted -> resync to %.1fs", m.position_at(now))
        elif act.event == "stalled":
            log.info("followed client still buffering at %.1fs", m.anchor_pos)

    def _follow_ghostless(self, now: float, act) -> None:
        """An app on this PC: nothing to launch, nothing to keep in step - the
        picture is already on a screen Hue Sync captures. Just light it."""
        self._idle_since = None
        self._paused_since = None
        if self._standby is not None:
            self._lights_on("%s is playing again" % (act.title or "the app"))
        if not self._engine_started:
            log.info("%s is playing on this PC -> engine start", _asc(act.title or "an app"))
            self.engine.start()
            self._engine_started = True
        est = self.engine.state()
        self.state = SYNCING if est.syncing else GHOSTING

    def _idle(self, now: float, act) -> None:
        if self.ghost is not None:
            if self._idle_since is None:
                self._idle_since = now
                log.info("followed client %s", "stopped" if act.seen else "gone")
            idle = now - self._idle_since
            close_after = float(self.cfg.get("sync.idle_stop_delay_s", 10.0))
            if self._standby != "stopped" and idle >= self.lights_off_delay:
                self._lights_off("stopped", now)
                log.info("lights off - TV stopped %.1fs ago; ghost on standby for %.0fs in case it comes back",
                         idle, max(0.0, close_after - idle))
            if idle >= close_after:
                self._stop_ghost("followed client idle")
        elif self._engine_started:
            # a PC source stopped: there is no ghost to park, only lights to stop
            if self._idle_since is None:
                self._idle_since = now
                log.info("nothing playing on this PC any more")
            if now - self._idle_since >= self.lights_off_delay:
                self.engine.stop()
                self._engine_started = False
                self._standby = None
                self._idle_since = None
                self.state = IDLE
                log.info("lights off")
        elif not self.enabled and self.state != IDLE:
            self.state = IDLE

    @property
    def lights_off_delay(self) -> float:
        return max(0.0, float(self.cfg.get("sync.lights_off_delay_s", 1.5) or 0.0))

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

    def _persist_stalls(self, now: float) -> None:
        st = self.watcher.stalls
        if not st.changed or now - self._stalls_saved_mono < 60 or not self.cfg.path:
            return
        st.changed = False
        self._stalls_saved_mono = now
        self.cfg.set("sync.stall_estimates", dict(st.est))
        try:
            self.cfg.save()
            log.info("learned client buffering: %s", {k: round(v, 1) for k, v in st.est.items()})
        except OSError as e:
            log.warning("could not persist stall estimates: %s", e)

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
        target = m.position_at(now) + self.offset
        held = m.paused or m.frozen(now)
        start = target + (0.0 if held else self._startup_latency)
        log.info("'%s' playing on %s -> ghost at %.1fs (offset %+.2fs, startup est %.2fs)",
                 _asc(m.name), _asc(obs.report.device_label if obs.report else "?"), start,
                 self.offset, self._startup_latency)
        if spec is None or not spec.audio_only:
            self._prepare_display()      # music has no picture: no display to wake
        extra = {"audio_only": True, "audio_device": spec.audio_device} if spec.audio_only else {}
        self._ghost_audio_only = bool(spec.audio_only)
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
        self._launches.append(now)
        self._launch_mono = now
        self._guard_logged = False
        self._engine_started = False
        self._pending_seek = False
        self._standby = None
        self.state = GHOSTING
        if held:
            self.ghost.apply([Pause()], now)

    def _prepare_display(self) -> None:
        """The displays may have been switched off by Windows' idle timeout (the
        virtual ghost display goes with them) - wake them before mpv draws its
        first frame, or Hue Sync captures a dead screen until someone touches
        the mouse. A locked PC cannot be captured at all: say so once."""
        if self.keep_awake_mode() != "off" and wake_display():
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
        if not g.alive():
            rc = g.proc.poll()
            reason = g.end_reason or "?"
            log.info("ghost mpv exited (rc=%s, reason=%s)", rc, reason)
            g.kill()
            self.ghost = None
            self.state = IDLE
            self.engine.stop()
            self._paused_since = None
            self._standby = None
            self._idle_since = None
            if reason == "eof":
                self._eof_item = g.item_id
            if g.user_quit:
                self.enabled = False
                log.info("ghost closed by user -> sync disabled (hue-ghost on / tray to re-enable)")
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

        if pause_stop_due(self._paused_since, now,
                          float(self.cfg.get("sync.pause_stop_min", 0.0) or 0.0),
                          self._standby is not None) and self._engine_started:
            log.info("client paused for %.0f min -> lights off (ghost holds, resumes on play)",
                     (now - self._paused_since) / 60.0)
            self._lights_off("paused", now)

        target = m.position_at(now) + self.offset
        target_held = m.paused or m.frozen(now)     # client paused, or on a still frame after a seek
        gobs = GhostObs(pos=g.pos, paused=g.paused, buffering=(g.buffering or g.stalled),
                        speed=g.speed, last_seek_mono=g.last_seek_mono)
        force = self._pending_seek and g.has_position
        actions = decide(target, target_held, gobs, self.params, now, force_seek=force)
        if force:
            self._pending_seek = False
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
            self.stats.add(gobs.pos - target, abs(g.speed - 1.0) > 1e-6)
        if self.stats.due(now):
            s = self.stats.flush()
            if s:
                log.info("sync quality (last %ds): mean|drift| %.3fs p95 %.3fs max %.3fs bias %+.3fs "
                         "seeks %d nudging %d/%d ticks", int(self.stats.period), s["mean_abs"],
                         s["p95_abs"], s["max_abs"], s["mean"], s["seeks"], s["nudge_ticks"], s["n"])

    def _stop_ghost(self, reason: str) -> None:
        g = self.ghost
        if g is None:
            return
        log.info("stopping ghost (%s)", reason)
        self.engine.stop()
        if self._engine_started:
            self.engine.wait_until(lambda s: not s.syncing or not s.connected, ENGINE_STOP_WAIT_S)
        g.kill()
        self.ghost = None
        self.state = IDLE
        self._engine_started = False
        self._idle_since = None
        self._paused_since = None
        self._standby = None
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
            log.info("binding '%s' %s", hit["name"] or hit["id"], "on" if on else "off")
        return {"id": hit["id"], "enabled": bool(on)}

    def status(self) -> dict:
        with self._lock:
            obs = self.last_obs
            act = self.last_act
            m = obs.model if obs else None
            now = time.monotonic()
            g = self.ghost
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
                    "standby_closes_in_s": (round(max(0.0, float(self.cfg.get("sync.idle_stop_delay_s", 10.0))
                                                      - (now - self._idle_since)), 1)
                                            if (g and self._idle_since is not None) else None),
                },
                "system": {
                    "keep_awake": self.keep_awake_mode(),
                    "awake_held": self._awake,
                    "locked": desktop_locked(),
                },
                "drift_s": round(drift, 3) if drift is not None else None,
                "drift_last_minute": self.stats.last_summary,
                "engine": self.engine.state().as_dict(),
                "offset_s": self.offset,
                "intensity": self.cfg.get("engine.huesync.intensity"),
                "mode": self.cfg.get("engine.huesync.mode"),
                "use_audio": self.cfg.get("engine.huesync.use_audio"),
                "manage_area": bool(self.cfg.get("engine.huesync.manage_area", True)),
                "lights_off_delay_s": self.lights_off_delay,
            }

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
            self.watcher.jitter_tol = float(self.cfg.get("sync.jitter_tolerance_s", 1.5))
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
        self._guard_logged = False
        self._launches.clear()

    def reload(self) -> None:
        self.cfg.reload()
        self.params = Params.from_config(self.cfg)
        self.watcher.jitter_tol = float(self.cfg.get("sync.jitter_tolerance_s", 1.5))
        self.watcher.poll_interval = float(self.cfg.get("jellyfin.poll_interval_s", 0.5))
        self._push_engine_prefs()
        log.info("config reloaded (offset %+.2fs, intensity %s)", self.offset,
                 self.cfg.get("engine.huesync.intensity"))

    def _push_engine_prefs(self) -> None:
        e = self.engine
        if self.cfg.get("engine.huesync.intensity"):
            try:
                e.set_intensity(self.cfg.get("engine.huesync.intensity"))
            except (ValueError, RuntimeError):
                pass
        if self.cfg.get("engine.huesync.mode"):
            try:
                e.set_mode(self.cfg.get("engine.huesync.mode"))
            except (ValueError, RuntimeError):
                pass
        try:
            e.set_use_audio(self.cfg.get("engine.huesync.use_audio"))
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

        Everything that shows mode and intensity - the window, the tray, the API,
        Home Assistant - reads them from the config, so persisting here is all it
        takes for the whole app to mirror Hue Sync. Deliberately no
        ``_push_engine_prefs()``: the engine already holds the value, and pushing
        it back is precisely the feedback loop this is meant to avoid."""
        got: dict = {}
        while True:
            try:
                got.update(self._engine_events.get_nowait())
            except queue.Empty:
                break
        if not got:
            return
        changed = False
        if got.get("mode") in MODES and self.cfg.get("engine.huesync.mode") != got["mode"]:
            self.cfg.set("engine.huesync.mode", got["mode"])
            changed = True
        if got.get("intensity") in INTENSITIES                 and self.cfg.get("engine.huesync.intensity") != got["intensity"]:
            self.cfg.set("engine.huesync.intensity", got["intensity"])
            changed = True
        if not changed:
            # e.g. Hue Sync's "scenes", which hue-ghost has no setting for: the
            # engine stops fighting it, but the saved config stays one we can run
            log.debug("Hue Sync is in %s; nothing hue-ghost stores, leaving the config alone", got)
            return
        log.info("adopted from the Hue Sync app: %s", got)
        if self.cfg.path:
            try:
                self.cfg.save()
            except OSError as e:
                log.warning("could not persist the adopted settings: %s", e)

    def _apply_settings(self, p: dict) -> None:
        changed = False
        if "enabled" in p:
            self.set_enabled(bool(p["enabled"]))
        if "intensity" in p:
            lvl = str(p["intensity"]).lower()
            if lvl not in INTENSITIES:
                raise ValueError("intensity must be one of " + ", ".join(INTENSITIES))
            self.cfg.set("engine.huesync.intensity", lvl)
            changed = True
        if "mode" in p:
            mode = str(p["mode"]).lower()
            if mode not in MODES:
                raise ValueError("mode must be one of " + ", ".join(MODES))
            self.cfg.set("engine.huesync.mode", mode)
            changed = True
        if "use_audio" in p:
            self.cfg.set("engine.huesync.use_audio", _tri_state(p["use_audio"]))
            changed = True
        if "manage_area" in p:
            self.cfg.set("engine.huesync.manage_area", bool(p["manage_area"]))
            changed = True
        if "pause_stop_min" in p:
            self.cfg.set("sync.pause_stop_min", max(0.0, round(float(p["pause_stop_min"]), 2)))
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
        if "offset_s" in p:
            self.cfg.set("sync.offset_s", round(float(p["offset_s"]), 3))
            changed = True
        if "offset_delta" in p:
            self.cfg.set("sync.offset_s", round(self.offset + float(p["offset_delta"]), 3))
            changed = True
        if "brightness_step" in p:
            self.engine.adjust_brightness(int(p["brightness_step"]))
        if "brightness" in p:
            level = int(p["brightness"])
            if not 0 <= level <= 100:
                raise ValueError("brightness must be 0-100")
            self.engine.set_brightness(level)
        if "binding" in p:
            b = p["binding"] or {}
            if not isinstance(b, dict) or "key" not in b:
                raise ValueError('binding must be {"key": "<id>", "enabled": true|false}')
            self.set_binding_enabled(str(b["key"]), bool(b.get("enabled", True)))
        if changed:
            self._push_engine_prefs()
            if self.cfg.path:
                try:
                    self.cfg.save()
                except OSError as e:
                    log.warning("could not persist settings: %s", e)
            log.info("settings: offset %+.2fs intensity %s", self.offset, self.cfg.get("engine.huesync.intensity"))
