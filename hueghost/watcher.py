"""Follow one Jellyfin session and model its true playback position.

Why a model at all: the followed client (Apple TV, phone, ...) only sends a
progress report to Jellyfin every few seconds, so ``PlayState.PositionTicks``
read from ``/Sessions`` is stale most of the time.  Re-anchoring the ghost on
every poll (v1 behaviour) turns that staleness into 1-4 s corrections.

The model here:
  * anchors ONLY when a new client report arrives (``(PositionTicks,
    LastPlaybackCheckIn)`` changed) and extrapolates at 1.0x in between;
  * maps the report's server timestamp to local time with a *clock-offset
    max-estimator*: every ``server_time - local_time`` sample we can get
    (``Date`` header truncated to seconds, check-in seen within one poll) is a
    lower bound of the true offset, so ``max(samples)`` converges to it;
  * treats a new report that lands within ``jitter_tolerance_s`` of the model
    as measurement noise (50 % blend) and anything beyond as a real seek.

Everything is pure w.r.t. I/O: ``observe()`` takes the session list and the
clocks; ``poll()`` is the thin HTTP wrapper.
"""
from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

from .jellyfin import TICKS_PER_S, JellyfinClient, item_display_name, parse_jf_datetime

MAX_INITIAL_AGE_S = 30.0


@dataclass
class Report:
    item_id: str
    media_source_id: str | None
    name: str
    pos: float
    paused: bool
    checkin: float | None          # server epoch of LastPlaybackCheckIn
    runtime_s: float | None
    device_id: str | None = None
    device_label: str = ""

    @property
    def key(self) -> tuple:
        return (round(self.pos, 3), self.checkin, self.paused)


class ClockOffset:
    """server_epoch - local_epoch, estimated as a running max of lower-bound samples.

    Every sample we can take is <= the true offset (Date headers are truncated
    to whole seconds; a check-in noticed now happened before now), so the max
    converges from below and never jumps the target around. If no sample has
    come within ``support_s`` of the max for ``stale_after_s`` (the server
    clock stepped backwards), the estimate is rebuilt from recent samples.
    """

    def __init__(self, stale_after_s: float = 300.0, support_s: float = 1.0):
        self.value: float | None = None
        self.samples = 0
        self._recent: deque[float] = deque(maxlen=64)
        self._support_t: float | None = None
        self.stale_after_s = stale_after_s
        self.support_s = support_s

    def add(self, server_epoch: float | None, local_epoch: float) -> None:
        if server_epoch is None:
            return
        sample = server_epoch - local_epoch
        self.samples += 1
        self._recent.append(sample)
        if self.value is None or sample > self.value:
            self.value = sample
            self._support_t = local_epoch
        elif sample > self.value - self.support_s:
            self._support_t = local_epoch
        elif self._support_t is not None and local_epoch - self._support_t > self.stale_after_s:
            self.value = max(self._recent)
            self._support_t = local_epoch


class SessionMatcher:
    def __init__(self, device_id: str = "", name_contains: str = "", user: str = "",
                 area_id: str = "", area_name: str = ""):
        self.device_id = (device_id or "").strip()
        self.needle = (name_contains or "").strip().lower()
        self.user = (user or "").strip().lower()
        self.area_id = (area_id or "").strip() or None
        self.area_name = (area_name or "").strip() or None

    @classmethod
    def from_player(cls, p: dict) -> "SessionMatcher":
        return cls(p.get("device_id", ""), p.get("device_name_contains", ""), p.get("user", ""),
                   p.get("area_id", ""), p.get("area_name", ""))

    def label(self) -> str:
        return self.device_id or self.needle or "?"

    def matches(self, s: dict) -> bool:
        if self.device_id:
            if (s.get("DeviceId") or "") != self.device_id:
                return False
        elif self.needle:
            label = ((s.get("DeviceName") or "") + " " + (s.get("Client") or "")).lower()
            if self.needle not in label:
                return False
        else:
            return False
        if self.user and self.user not in (s.get("UserName") or "").lower():
            return False
        return True

    def pick(self, sessions: Iterable[dict]) -> dict | None:
        best = None
        for s in sessions or []:
            if not self.matches(s):
                continue
            if s.get("NowPlayingItem"):
                return s
            best = best or s
        return best


class PlayerSet:
    """Several players in priority order; the first one that is playing wins."""

    def __init__(self, matchers: list[SessionMatcher]):
        self.matchers = matchers

    @classmethod
    def from_players(cls, players: list[dict]) -> "PlayerSet":
        return cls([SessionMatcher.from_player(p) for p in players])

    def pick(self, sessions: Iterable[dict]) -> tuple[dict | None, SessionMatcher | None]:
        sessions = list(sessions or [])
        first_seen: tuple[dict, SessionMatcher] | None = None
        for m in self.matchers:
            s = m.pick(sessions)
            if s is None:
                continue
            if report_from_session(s) is not None:
                return s, m
            first_seen = first_seen or (s, m)
        return first_seen if first_seen else (None, None)


def report_from_session(s: dict) -> Report | None:
    np = s.get("NowPlayingItem")
    ps = s.get("PlayState") or {}
    if not np or np.get("MediaType") != "Video":
        return None
    ticks = ps.get("PositionTicks")
    if ticks is None:
        return None
    rt = np.get("RunTimeTicks")
    return Report(
        item_id=str(np.get("Id")),
        media_source_id=ps.get("MediaSourceId") or None,
        name=item_display_name(np),
        pos=float(ticks) / TICKS_PER_S,
        paused=bool(ps.get("IsPaused")),
        checkin=parse_jf_datetime(s.get("LastPlaybackCheckIn")),
        runtime_s=(float(rt) / TICKS_PER_S) if rt else None,
        device_id=s.get("DeviceId"),
        device_label="%s / %s" % (s.get("DeviceName") or "?", s.get("Client") or "?"),
    )


STALL_PRIORS = {"seek": 3.5, "start": 3.5, "resume": 1.0}   # seconds, learned per client
STALL_MAX_S = 15.0
RESIDUAL_WINDOW = 6
RESIDUAL_GAIN = 0.5
RESIDUAL_MAX_STEP = 0.25


class StallEstimator:
    """How long the followed client sits on a still frame after a seek / start /
    resume before its position actually advances (buffering). Learned per event
    kind from the first report that shows the position moving again."""

    def __init__(self, priors: dict[str, float] | None = None):
        self.est: dict[str, float] = dict(STALL_PRIORS)
        if priors:
            for k, v in priors.items():
                if k in self.est and isinstance(v, (int, float)):
                    self.est[k] = min(max(float(v), 0.0), STALL_MAX_S)
        self.changed = False

    def predict(self, kind: str) -> float:
        return self.est.get(kind, 0.0)

    def learn(self, kind: str, measured: float) -> None:
        measured = min(max(measured, 0.0), STALL_MAX_S)
        self.est[kind] = round(0.6 * self.est.get(kind, measured) + 0.4 * measured, 3)
        self.changed = True

    def at_least(self, kind: str, lower: float) -> None:
        if lower > self.est.get(kind, 0.0):
            self.est[kind] = round(min(lower, STALL_MAX_S), 3)
            self.changed = True


@dataclass
class PlaybackModel:
    item_id: str
    media_source_id: str | None
    name: str
    runtime_s: float | None
    paused: bool
    anchor_pos: float
    anchor_mono: float          # extrapolation starts here (later than the report while stalled)
    stalls: StallEstimator
    last_key: tuple = field(default_factory=tuple)
    reports: int = 0
    last_report_mono: float = 0.0
    residuals: deque = field(default_factory=lambda: deque(maxlen=RESIDUAL_WINDOW))
    stall_kind: str | None = None      # a stall window awaiting confirmation
    stall_pos: float = 0.0
    stall_report_mono: float = 0.0
    last_debug: str = ""
    device_id: str | None = None

    # -- queries ---------------------------------------------------------------------
    def position_at(self, now_mono: float) -> float:
        if self.paused:
            return self.anchor_pos
        return self.anchor_pos + max(0.0, now_mono - self.anchor_mono)

    def frozen(self, now_mono: float) -> bool:
        """Playing, but the client is (predicted to be) on a still frame."""
        return (not self.paused) and now_mono < self.anchor_mono

    # -- internals -------------------------------------------------------------------
    def _anchor(self, pos: float, mono: float) -> None:
        self.anchor_pos = float(pos)
        self.anchor_mono = float(mono)

    def begin_stall(self, kind: str, pos: float, report_mono: float) -> None:
        self._anchor(pos, report_mono + self.stalls.predict(kind))
        self.stall_kind = kind
        self.stall_pos = float(pos)
        self.stall_report_mono = report_mono
        self.residuals.clear()

    def apply(self, r: Report, report_mono: float, jitter_tol: float) -> str | None:
        """Fold a (new) report in. Returns an event name or None."""
        if r.key == self.last_key:
            return None
        self.last_key = r.key
        self.reports += 1
        self.last_report_mono = report_mono
        self.name = r.name
        if r.media_source_id:
            self.media_source_id = r.media_source_id
        pred = self.position_at(report_mono)
        delta = r.pos - pred
        event: str | None = None

        if r.paused and not self.paused:
            self.paused = True
            self.stall_kind = None
            self._anchor(r.pos, report_mono)
            event = "pause"
        elif not r.paused and self.paused:
            self.paused = False
            self.begin_stall("resume", r.pos, report_mono)
            event = "resume"
        elif r.paused:
            if abs(r.pos - self.anchor_pos) > jitter_tol:
                event = "seek"
            self._anchor(r.pos, report_mono)
        elif self.stall_kind is not None:
            advanced = r.pos - self.stall_pos
            elapsed = report_mono - self.stall_report_mono
            if advanced < 0.5:
                # still on the still frame: the stall is at least this long
                self.stalls.at_least(self.stall_kind, elapsed)
                self._anchor(r.pos, max(self.anchor_mono, report_mono + 1.0))
                event = "stalled"
            else:
                measured = elapsed - advanced
                self.stalls.learn(self.stall_kind, measured)
                self.stall_kind = None
                self._anchor(r.pos, report_mono)
                if abs(delta) > jitter_tol:
                    event = "resync"       # stall guess was off by more than the tolerance
        else:
            if abs(delta) > jitter_tol:
                self.begin_stall("seek", r.pos, report_mono)
                event = "seek"
            else:
                # steady state: the rate is exactly 1.0, only the intercept is
                # uncertain - correct it slowly from the median residual so
                # per-report position jitter does not reach the ghost
                self.residuals.append(delta)
                med = statistics.median(self.residuals)
                corr = max(-RESIDUAL_MAX_STEP, min(RESIDUAL_MAX_STEP, med)) * RESIDUAL_GAIN
                self._anchor(pred + corr, report_mono)
                self.residuals = deque((x - corr for x in self.residuals), maxlen=RESIDUAL_WINDOW)
        self.last_debug = ("report #%d pos=%.2f%s pred=%.2f delta=%+.2f%s stall=%s" % (
            self.reports, r.pos, " paused" if r.paused else "", pred, delta,
            (" -> " + event) if event else "",
            {k: round(v, 1) for k, v in self.stalls.est.items()}))
        return event


@dataclass
class Observation:
    seen: bool                     # followed device has a session at all
    playing: bool                  # it is playing a video (paused counts)
    model: PlaybackModel | None
    event: str | None              # new_item | seek | pause | resume | None
    report: Report | None
    stale: bool = False            # first report was older than MAX_INITIAL_AGE_S
    player: SessionMatcher | None = None   # which followed player this is


class SessionWatcher:
    def __init__(self, matcher: "SessionMatcher | PlayerSet", jitter_tolerance_s: float = 1.5,
                 poll_interval_s: float = 0.5, stall_priors: dict[str, float] | None = None):
        self.players = matcher if isinstance(matcher, PlayerSet) else PlayerSet([matcher])
        self.matcher = self.players.matchers[0] if self.players.matchers else matcher
        self.jitter_tol = float(jitter_tolerance_s)
        self.poll_interval = float(poll_interval_s)
        self.clock = ClockOffset()
        self.stalls = StallEstimator(stall_priors)
        self.model: PlaybackModel | None = None
        self._prev_poll_mono: float | None = None
        self._playing_last_poll = False
        self.last_error: str | None = None

    # -- pure core --------------------------------------------------------
    def observe(self, sessions: list[dict], server_epoch: float | None,
                local_epoch: float, now_mono: float) -> Observation:
        self.clock.add(server_epoch, local_epoch)
        s, who = self.players.pick(sessions)
        prev_poll = self._prev_poll_mono
        self._prev_poll_mono = now_mono
        was_playing = self._playing_last_poll
        self._playing_last_poll = False
        if s is None:
            self.model = None
            return Observation(False, False, None, None, None)
        r = report_from_session(s)
        if r is None:
            self.model = None
            return Observation(True, False, None, None, None, player=who)
        self._playing_last_poll = True

        first = (self.model is None or self.model.item_id != r.item_id
                 or (self.model.device_id or None) != (r.device_id or None))
        report_mono, stale = self._report_time(r, local_epoch, now_mono, prev_poll, first)
        if first:
            self.model = PlaybackModel(
                item_id=r.item_id, media_source_id=r.media_source_id, name=r.name,
                runtime_s=r.runtime_s, paused=r.paused,
                anchor_pos=r.pos, anchor_mono=report_mono, stalls=self.stalls,
                last_key=r.key, reports=1, last_report_mono=report_mono, device_id=r.device_id)
            # playback that just appeared (not: the daemon started mid-movie)
            # is a fresh start: the client reports its position, then buffers
            just_started = prev_poll is not None and not was_playing and (now_mono - report_mono) < 3.0
            if just_started and not r.paused:
                self.model.begin_stall("start", r.pos, report_mono)
            self.model.last_debug = "report #1 pos=%.2f age=%.2fs%s" % (
                r.pos, now_mono - report_mono, " (start stall %.1fs)" % self.stalls.predict("start") if just_started else "")
            return Observation(True, True, self.model, "new_item", r, stale, player=who)
        event = self.model.apply(r, report_mono, self.jitter_tol)
        return Observation(True, True, self.model, event, r, False, player=who)

    def _report_time(self, r: Report, local_epoch: float, now_mono: float,
                     prev_poll: float | None, first: bool) -> tuple[float, bool]:
        """Local monotonic time at which the client took this report."""
        fallback = now_mono if first else now_mono - self.poll_interval / 2.0
        if r.checkin is None:
            return fallback, False
        if not first:
            # the report was not there at the previous poll, so it happened
            # before *now*: checkin - now is a valid lower bound of the offset
            self.clock.add(r.checkin, local_epoch)
        off = self.clock.value
        if off is None:
            return fallback, False
        age = local_epoch - (r.checkin - off)   # seconds since the report
        if first:
            stale = age > MAX_INITIAL_AGE_S
            age = min(max(age, 0.0), MAX_INITIAL_AGE_S)
            return now_mono - age, stale
        lo = prev_poll if prev_poll is not None else now_mono - self.poll_interval
        age = min(max(age, 0.0), max(0.0, now_mono - lo))
        return now_mono - age, False

    # -- I/O wrapper ------------------------------------------------------
    def poll(self, client: JellyfinClient) -> Observation:
        sessions, server_epoch, local_epoch = client.sessions()
        return self.observe(sessions, server_epoch, local_epoch, time.monotonic())

    def reset(self) -> None:
        self.model = None
