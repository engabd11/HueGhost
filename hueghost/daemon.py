"""The orchestrator: watch the followed Jellyfin session, keep the ghost in
lockstep, and turn the light engine on/off at the right moments.

States:  idle -> ghosting (mpv up, waiting for / has frames) -> syncing (engine reports sync)

Ordering rules (avoid flashing the desktop colours into the living room):
  start: launch mpv -> first time-pos seen -> engine.start()
  stop:  engine.stop() -> wait until not syncing (<= 2 s) -> kill mpv
"""
from __future__ import annotations

import logging
import os
import statistics
import threading
import time
from collections import deque

from . import __version__
from .config import Config, INTENSITIES, app_data_dir, source_root
from .control import ControlServer
from .engines import Engine, build_engine
from .ghost import GhostPlayer
from .jellyfin import JellyfinClient, JellyfinError
from .lockstep import GhostObs, Params, Pause, Resume, Seek, decide
from .watcher import Observation, SessionMatcher, SessionWatcher

log = logging.getLogger("hue-ghost")

IDLE, GHOSTING, SYNCING = "idle", "ghosting", "syncing"
TICK_S = 0.25
ENGINE_STOP_WAIT_S = 2.0


def _asc(s) -> str:
    return (s or "").encode("ascii", "replace").decode("ascii")


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
        self.jf = JellyfinClient(cfg.get("jellyfin.url"), cfg.get("jellyfin.api_key"))
        self.watcher = self._make_watcher()
        self.params = Params.from_config(cfg)
        self.engine: Engine = build_engine(cfg)
        self.ghost: GhostPlayer | None = None
        self.enabled = bool(cfg.get("enabled", True))
        self.state = IDLE
        self.stats = DriftStats()
        self.last_obs: Observation | None = None
        self.jf_ok = False
        self.jf_error: str | None = None
        self._jf_err_log = 0.0
        self._required_log = 0.0
        self._idle_since: float | None = None
        self._launches: deque[float] = deque()
        self._last_launch = 0.0
        self._last_launch_item: str | None = None
        self._guard_logged = False
        self._pending_seek = False
        self._engine_started = False
        self._eof_item: str | None = None
        self._startup_latency = 1.0   # EMA of launch -> first time-pos
        self._launch_mono = 0.0
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.control: ControlServer | None = None
        base = source_root() or app_data_dir()
        os.makedirs(base, exist_ok=True)
        self._mpv_err = os.path.join(base, "mpv_ghost.err")

    # -- setup ----------------------------------------------------------------
    def _make_watcher(self) -> SessionWatcher:
        f = self.cfg.get("jellyfin.follow", {}) or {}
        return SessionWatcher(
            SessionMatcher(f.get("device_id", ""), f.get("device_name_contains", ""), f.get("user", "")),
            jitter_tolerance_s=float(self.cfg.get("sync.jitter_tolerance_s", 0.75)),
            poll_interval_s=float(self.cfg.get("jellyfin.poll_interval_s", 0.5)))

    def _start_control(self) -> None:
        c = self.cfg.section("control")
        port = int(c.get("port") or 0)
        if not port:
            return
        try:
            self.control = ControlServer(c.get("bind", "127.0.0.1"), port, c.get("token", ""),
                                         self.status, self.action)
            self.control.start()
        except OSError as e:
            log.warning("control API unavailable on port %s: %s", port, e)
            self.control = None

    # -- main loop --------------------------------------------------------------
    def run(self) -> None:
        self._start_control()
        f = self.cfg.get("jellyfin.follow", {}) or {}
        log.info("hue-ghost %s following %s on %s (engine=%s, poll %.2fs, offset %+.2fs)",
                 __version__, f.get("device_id") or repr(f.get("device_name_contains")),
                 self.cfg.get("jellyfin.url"), self.engine.name,
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
                wait = max(0.05, min(TICK_S, next_poll - time.monotonic()))
                self._stop.wait(wait)
        finally:
            self._shutdown()

    def stop(self) -> None:
        self._stop.set()

    def _shutdown(self) -> None:
        with self._lock:
            if self.ghost is not None:
                self._stop_ghost("daemon exit")
            try:
                self.engine.stop()
                self.engine.wait_until(lambda s: not s.syncing or not s.connected, ENGINE_STOP_WAIT_S)
            finally:
                self.engine.close()
        if self.control:
            self.control.stop()
        log.info("stopped")

    @property
    def offset(self) -> float:
        return float(self.cfg.get("sync.offset_s", 0.0))

    # -- polling the followed session --------------------------------------------
    def _poll(self, now: float) -> None:
        try:
            obs = self.watcher.poll(self.jf)
            if not self.jf_ok:
                log.info("Jellyfin reachable")
            self.jf_ok, self.jf_error = True, None
        except JellyfinError as e:
            self.jf_ok, self.jf_error = False, str(e)
            if now - self._jf_err_log > 30:
                log.warning("Jellyfin unreachable (%s) - keeping ghost as-is", _asc(str(e)))
                self._jf_err_log = now
            return
        self.last_obs = obs

        if obs.playing and obs.model is not None and self.enabled:
            self._idle_since = None
            m = obs.model
            if obs.event == "new_item" and obs.stale:
                log.warning("first report for '%s' is stale (>30 s old); position may be off", _asc(m.name))
            if self.ghost is not None and self.ghost.item_id != m.item_id:
                log.info("followed client switched to '%s' -> relaunching ghost", _asc(m.name))
                self._stop_ghost("item changed")
            if self.ghost is None:
                self._maybe_launch(now, obs)
            elif obs.event in ("seek", "resume", "new_item"):
                self._pending_seek = True
                if obs.event == "seek":
                    log.info("followed client seeked to %.1fs", m.position_at(now))
        else:
            if self.ghost is not None:
                if self._idle_since is None:
                    self._idle_since = now
                    log.info("followed client %s", "stopped" if obs.seen else "gone")
                if now - self._idle_since >= float(self.cfg.get("sync.idle_stop_delay_s", 10.0)):
                    self._stop_ghost("followed client idle")
            elif not self.enabled and self.state != IDLE:
                self.state = IDLE

    def _maybe_launch(self, now: float, obs: Observation) -> None:
        m = obs.model
        assert m is not None
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
        start = target + (0.0 if m.paused else self._startup_latency)
        log.info("'%s' playing on %s -> ghost at %.1fs (offset %+.2fs, startup est %.2fs)",
                 _asc(m.name), _asc(obs.report.device_label if obs.report else "?"), start,
                 self.offset, self._startup_latency)
        url = self.jf.stream_url(m.item_id, m.media_source_id)
        try:
            self.ghost = GhostPlayer.launch(self.cfg, url, self.jf.auth_header_for_mpv(), start,
                                            m.item_id, err_path=self._mpv_err)
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
        self.state = GHOSTING
        if m.paused:
            self.ghost.apply([Pause()], now)

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
            if reason == "eof":
                self._eof_item = g.item_id
            if g.user_quit:
                self.enabled = False
                log.info("ghost closed by user -> sync disabled (hue-ghost on / tray to re-enable)")
            return

        obs = self.last_obs
        m = obs.model if obs else None
        if m is None:
            return

        if g.has_position and not self._engine_started:
            lat = now - self._launch_mono
            self._startup_latency = 0.7 * self._startup_latency + 0.3 * min(lat, 5.0)
            log.info("ghost rendering (startup %.2fs) -> engine start", lat)
            self.engine.start()
            self._engine_started = True

        est = self.engine.state()
        self.state = SYNCING if (self._engine_started and est.syncing) else GHOSTING

        target = m.position_at(now) + self.offset
        gobs = GhostObs(pos=g.pos, paused=g.paused, buffering=(g.buffering or g.stalled),
                        speed=g.speed, last_seek_mono=g.last_seek_mono)
        force = self._pending_seek and g.has_position
        actions = decide(target, m.paused, gobs, self.params, now, force_seek=force)
        if force:
            self._pending_seek = False
        if actions:
            g.apply(actions, now)
            for a in actions:
                if isinstance(a, Seek):
                    d = (gobs.pos - target) if gobs.pos is not None else float("nan")
                    log.info("drift %+.2fs -> seek to %.1fs%s", d, a.pos, " (client event)" if force else "")
                    self.stats.seeks += 1
                elif isinstance(a, Pause):
                    log.info("followed client paused -> ghost paused")
                elif isinstance(a, Resume):
                    log.info("followed client resumed -> ghost resumed at %.1fs", target)
        if gobs.pos is not None and not m.paused and not gobs.buffering and not gobs.paused:
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
        s = self.stats.flush()
        if s:
            log.info("sync quality (session tail): mean|drift| %.3fs p95 %.3fs max %.3fs seeks %d",
                     s["mean_abs"], s["p95_abs"], s["max_abs"], s["seeks"])

    # -- control surface ----------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            obs = self.last_obs
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
                "jellyfin": {"url": self.cfg.get("jellyfin.url"), "ok": self.jf_ok, "error": self.jf_error,
                             "clock_offset_s": self.watcher.clock.value},
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
                    "reports": m.reports if m else 0,
                },
                "ghost": {
                    "alive": bool(g and g.alive()),
                    "position_s": round(g.pos, 2) if (g and g.pos is not None) else None,
                    "paused": g.paused if g else None,
                    "buffering": (g.buffering or g.stalled) if g else None,
                    "speed": g.speed if g else None,
                    "seeks": g.seeks if g else 0,
                    "nudges": g.nudges if g else 0,
                },
                "drift_s": round(drift, 3) if drift is not None else None,
                "drift_last_minute": self.stats.last_summary,
                "engine": self.engine.state().as_dict(),
                "offset_s": self.offset,
                "intensity": self.cfg.get("engine.huesync.intensity"),
                "mode": self.cfg.get("engine.huesync.mode"),
            }

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
        self.watcher.jitter_tol = float(self.cfg.get("sync.jitter_tolerance_s", 0.75))
        self.watcher.poll_interval = float(self.cfg.get("jellyfin.poll_interval_s", 0.5))
        self._push_engine_prefs()
        log.info("config reloaded (offset %+.2fs, intensity %s)", self.offset,
                 self.cfg.get("engine.huesync.intensity"))

    def _push_engine_prefs(self) -> None:
        e = self.engine
        if hasattr(e, "set_intensity") and self.cfg.get("engine.huesync.intensity"):
            try:
                e.set_intensity(self.cfg.get("engine.huesync.intensity"))
            except (ValueError, RuntimeError):
                pass
        if hasattr(e, "set_mode") and self.cfg.get("engine.huesync.mode"):
            try:
                e.set_mode(self.cfg.get("engine.huesync.mode"))  # type: ignore[attr-defined]
            except (ValueError, RuntimeError):
                pass

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
            self.cfg.set("engine.huesync.mode", str(p["mode"]).lower())
            changed = True
        if "offset_s" in p:
            self.cfg.set("sync.offset_s", round(float(p["offset_s"]), 3))
            changed = True
        if "offset_delta" in p:
            self.cfg.set("sync.offset_s", round(self.offset + float(p["offset_delta"]), 3))
            changed = True
        if "brightness_step" in p:
            self.engine.adjust_brightness(int(p["brightness_step"]))
        if changed:
            self._push_engine_prefs()
            if self.cfg.path:
                try:
                    self.cfg.save()
                except OSError as e:
                    log.warning("could not persist settings: %s", e)
            log.info("settings: offset %+.2fs intensity %s", self.offset, self.cfg.get("engine.huesync.intensity"))
