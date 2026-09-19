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
    def __init__(self, device_id: str = "", name_contains: str = "", user: str = ""):
        self.device_id = (device_id or "").strip()
        self.needle = (name_contains or "").strip().lower()
        self.user = (user or "").strip().lower()

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


@dataclass
class PlaybackModel:
    item_id: str
    media_source_id: str | None
    name: str
    runtime_s: float | None
    paused: bool
    anchor_pos: float
    anchor_mono: float
    last_key: tuple = field(default_factory=tuple)
    reports: int = 0
    last_report_mono: float = 0.0

    def position_at(self, now_mono: float) -> float:
        if self.paused:
            return self.anchor_pos
        return self.anchor_pos + (now_mono - self.anchor_mono)

    def _anchor(self, pos: float, mono: float) -> None:
        self.anchor_pos = float(pos)
        self.anchor_mono = float(mono)

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
        event: str | None = None
        if r.paused and not self.paused:
            self.paused = True
            self._anchor(r.pos, report_mono)
            event = "pause"
        elif not r.paused and self.paused:
            self.paused = False
            self._anchor(r.pos, report_mono)
            event = "resume"
        elif r.paused:
            if abs(r.pos - self.anchor_pos) > jitter_tol:
                event = "seek"
            self._anchor(r.pos, report_mono)
        else:
            pred = self.position_at(report_mono)
            delta = r.pos - pred
            if abs(delta) <= jitter_tol:
                self._anchor(pred + 0.5 * delta, report_mono)
            else:
                self._anchor(r.pos, report_mono)
                event = "seek"
        return event


@dataclass
class Observation:
    seen: bool                     # followed device has a session at all
    playing: bool                  # it is playing a video (paused counts)
    model: PlaybackModel | None
    event: str | None              # new_item | seek | pause | resume | None
    report: Report | None
    stale: bool = False            # first report was older than MAX_INITIAL_AGE_S


class SessionWatcher:
    def __init__(self, matcher: SessionMatcher, jitter_tolerance_s: float = 0.75,
                 poll_interval_s: float = 0.5):
        self.matcher = matcher
        self.jitter_tol = float(jitter_tolerance_s)
        self.poll_interval = float(poll_interval_s)
        self.clock = ClockOffset()
        self.model: PlaybackModel | None = None
        self._prev_poll_mono: float | None = None
        self.last_error: str | None = None

    # -- pure core --------------------------------------------------------
    def observe(self, sessions: list[dict], server_epoch: float | None,
                local_epoch: float, now_mono: float) -> Observation:
        self.clock.add(server_epoch, local_epoch)
        s = self.matcher.pick(sessions)
        prev_poll = self._prev_poll_mono
        self._prev_poll_mono = now_mono
        if s is None:
            self.model = None
            return Observation(False, False, None, None, None)
        r = report_from_session(s)
        if r is None:
            self.model = None
            return Observation(True, False, None, None, None)

        first = self.model is None or self.model.item_id != r.item_id
        report_mono, stale = self._report_time(r, local_epoch, now_mono, prev_poll, first)
        if first:
            self.model = PlaybackModel(
                item_id=r.item_id, media_source_id=r.media_source_id, name=r.name,
                runtime_s=r.runtime_s, paused=r.paused,
                anchor_pos=r.pos, anchor_mono=report_mono, last_key=r.key, reports=1,
                last_report_mono=report_mono)
            return Observation(True, True, self.model, "new_item", r, stale)
        event = self.model.apply(r, report_mono, self.jitter_tol)
        return Observation(True, True, self.model, event, r, False)

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
