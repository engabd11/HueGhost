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
    media_type: str = "video"      # video | music

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
                 area_id: str = "", area_name: str = "", kinds: tuple = ("video", "music"),
                 client: str = "", binding_id: str = "", claimed: Iterable[str] = ()):
        self.device_id = (device_id or "").strip()
        self.binding_id = binding_id or ""
        # device ids that belong to other bindings - disabled ones included: a
        # name-only binding must not pick up a device someone switched off
        self.claimed = frozenset(c for c in claimed if c and c != self.device_id)
        self.needle = (name_contains or "").strip().lower()
        self.client = (client or "").strip().lower()
        self.user = (user or "").strip().lower()
        self.area_id = (area_id or "").strip() or None
        self.area_name = (area_name or "").strip() or None
        self.kinds = tuple(kinds) or ("video", "music")

    @classmethod
    def from_player(cls, p: dict, claimed: Iterable[str] = ()) -> "SessionMatcher":
        return cls(p.get("device_id", ""), p.get("device_name_contains", ""), p.get("user", ""),
                   p.get("area_id", ""), p.get("area_name", ""),
                   tuple(p.get("kinds") or ("video", "music")), p.get("client", ""),
                   binding_id=p.get("id", ""), claimed=claimed)

    def wants(self, r: "Report | None") -> bool:
        """A player followed for films only should not light up for an album."""
        return r is not None and r.media_type in self.kinds

    def label(self) -> str:
        return self.device_id or self.needle or "?"

    def matches(self, s: dict, by_name: bool = True) -> bool:
        did = s.get("DeviceId") or ""
        if not (self.device_id and did == self.device_id):
            # Names are generic - every phone calls itself "Android" - so a
            # name never takes a device another binding owns, switched off or
            # not: that is how one phone's binding followed another phone whose
            # own binding was disabled.
            # Jellyfin for Android form-encodes its name: "Abdullah's+S23"
            label = ((s.get("DeviceName") or "") + " " + (s.get("Client") or "")).replace("+", " ").lower()
            needle = self.needle.replace("+", " ")
            if not (by_name and needle and needle in label) or did in self.claimed:
                return False
        # Narrowing by app is what separates two entries a phone reports under
        # the same generic device name - music in one app, films in another.
        if self.client and self.client not in (s.get("Client") or "").lower():
            return False
        if self.user and self.user not in (s.get("UserName") or "").lower():
            return False
        return True

    def pick(self, sessions: Iterable[dict]) -> dict | None:
        sessions = list(sessions or [])
        # the name is the fallback for a device id that was regenerated (an
        # app reinstall): not while the device itself is right there
        own = bool(self.device_id) and any((s.get("DeviceId") or "") == self.device_id for s in sessions)
        best = None
        for s in sessions:
            if not self.matches(s, by_name=not own):
                continue
            if s.get("NowPlayingItem"):
                return s
            best = best or s
        return best


class PlayerSet:
    """Several players in priority order; the first one that is playing wins.

    A paused player never holds the lights against one that plays, whatever
    their order (Abdullah, 2026-10-19: CAMusic-linux sat paused above his phone
    and the phone's music stayed dark for 15 s). It takes them straight back
    when it plays again - it is still higher in the list. With nothing else
    playing, a paused player keeps its place, so it resumes where it was."""

    def __init__(self, matchers: list[SessionMatcher]):
        self.matchers = matchers

    @classmethod
    def from_players(cls, players: list[dict], claimed: Iterable[str] | None = None) -> "PlayerSet":
        claimed = set(claimed if claimed is not None else (p.get("device_id") for p in players))
        return cls([SessionMatcher.from_player(p, claimed) for p in players])

    def pick(self, sessions: Iterable[dict], now: float | None = None
             ) -> tuple[dict | None, SessionMatcher | None]:
        """The first player in list order that plays something it follows; a
        paused one only when no player is playing. (``now`` is unused; kept
        for callers.)"""
        sessions = list(sessions or [])
        first_seen: tuple[dict, SessionMatcher] | None = None
        first_paused: tuple[dict, SessionMatcher] | None = None
        for m in self.matchers:
            s = m.pick(sessions)
            if s is None:
                continue
            r = report_from_session(s)
            if m.wants(r):
                if r.paused:
                    first_paused = first_paused or (s, m)
                    continue
                return s, m
            first_seen = first_seen or (s, m)
        if first_paused:
            return first_paused
        return first_seen if first_seen else (None, None)


MEDIA_KINDS = {"Video": "video", "Audio": "music"}


def report_from_session(s: dict) -> Report | None:
    np = s.get("NowPlayingItem")
    ps = s.get("PlayState") or {}
    if not np:
        return None
    kind = MEDIA_KINDS.get(np.get("MediaType") or "")
    if kind is None:
        return None          # a photo, a book: nothing with a position to follow
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
        media_type=kind,
    )


STALL_PRIORS = {"seek": 3.5, "start": 3.5, "resume": 1.0}   # seconds, learned per client
STALL_MAX_S = 15.0
RESIDUAL_WINDOW = 6
RESIDUAL_GAIN = 0.5
RESIDUAL_MAX_STEP = 0.25
SHARP_NOISE_S = 0.03       # timed reports this consistent are steered by in full


ENVELOPE_WINDOW_S = 150.0  # a lagging player's envelope is the top of this much of its reports
LAG_TOL_S = 0.4            # ... which sit at most this far under it, or the player re-buffered
LAG_TOL_REPORTS = 2
LIFT_LOG_S = 0.05          # an envelope lift this big is worth a log line
CLASSIFY_EVERY = 30        # steady timed reports between two looks at a player's profile
CLASSIFY_SPAN_S = 40.0
CLASSIFY_SLOW_SPAN_S = 80.0  # ... or after this long, for a player with few timed reports
BIG_STEP_S = 0.05
SAWTOOTH_RATIO = 12.0      # envelope depth over the per-report change, for a sawtooth
NOISE_RATIO = 8.0          # ... and below this, plain symmetric noise
PROFILE_VOTES = 2          # agreeing looks that settle (or flip) a player's kind


def despike(ys: list[float]) -> list[float]:
    """Drop single reports that jump away and straight back (CAMusic-linux
    sends one a second off now and then: +1.0 s, then on the line again)."""
    out = []
    for i, y in enumerate(ys):
        if 0 < i < len(ys) - 1:
            a, b = y - ys[i - 1], ys[i + 1] - y
            if abs(a) > BIG_STEP_S and abs(b) > BIG_STEP_S and (a > 0) != (b > 0) \
                    and abs(a + b) < 0.5 * min(abs(a), abs(b)):
                continue
        out.append(y)
    return out


def envelope_top(ys: list[float]) -> float:
    """The top of a lagging player's reports, as far as two reports in a row
    agree on it: a tooth top is followed by reports just under it, a single
    stray report is not - one alone never lifts the timeline."""
    if len(ys) < 2:
        return ys[-1]
    return max(min(a, b) for a, b in zip(ys, ys[1:]))


@dataclass
class ReportProfile:
    """How one player's position reports err - learned, kept per device.

    ``symmetric``: reports scatter both ways around the truth (Moonfin, ~4 ms):
    the median filter is right. ``lagging``: reports only ever fall behind and
    then catch up in steps - CAMusic-linux forwards MPD's ``elapsed``, which
    lost 2.44 ms/s and jumped +0.22 s every 70-100 s (live, 2026-10-19). Its
    true position is the TOP of its reports; following the reports themselves
    dragged the ghost up and down that sawtooth and released the time lock
    every half minute."""
    kind: str = "unknown"          # unknown | symmetric | lagging
    noise_s: float = 0.0           # median change between two steady timed reports
    lag_depth_s: float = 0.0       # mean gap under the envelope (lagging)
    interval_s: float = 0.0        # report cadence
    votes: int = 0                 # > 0 leans lagging, < 0 symmetric
    changed: bool = False

    @classmethod
    def from_dict(cls, d) -> "ReportProfile":
        p = cls()
        if isinstance(d, dict):
            if d.get("kind") in ("symmetric", "lagging"):
                p.kind = d["kind"]
                p.votes = PROFILE_VOTES if p.kind == "lagging" else -PROFILE_VOTES
            for k in ("noise_s", "lag_depth_s", "interval_s"):
                if isinstance(d.get(k), (int, float)):
                    setattr(p, k, min(max(float(d[k]), 0.0), 5.0))
        return p

    def to_dict(self) -> dict:
        return {"kind": self.kind, "noise_s": round(self.noise_s, 4),
                "lag_depth_s": round(self.lag_depth_s, 3), "interval_s": round(self.interval_s, 2)}

    def look(self, pts: list[tuple[float, float]]) -> None:
        """One look at a steady stretch of ``(report_mono, raw)`` - ``raw`` is
        the report against a fixed 1.0x line - and a vote on what it shows."""
        if len(pts) < 8 or pts[-1][0] - pts[0][0] < CLASSIFY_SPAN_S:
            return
        ys = despike([y for _, y in pts])
        ds = [b - a for a, b in zip(ys, ys[1:])]
        step = max(statistics.median(abs(d) for d in ds), 0.001)
        big = max(BIG_STEP_S, 6.0 * step)
        up = sum(d for d in ds if d > big)
        down = -sum(d for d in ds if d < -big)
        depth = max(ys) - min(ys)
        # a sawtooth also LOSES ground between its steps; steps up alone are
        # only the clock-offset estimate getting better
        decline = -sum(d for d in ds if abs(d) <= big)
        before = (self.kind, self.noise_s, self.lag_depth_s)
        vote = 0
        if up > 0 and up >= 3.0 * down and depth / step >= SAWTOOTH_RATIO \
                and decline >= max(BIG_STEP_S, 0.5 * up):
            vote = 1
        elif up == 0 and down == 0 and depth / step < NOISE_RATIO:
            vote = -1
        self.noise_s = round(0.7 * self.noise_s + 0.3 * step, 4) if self.noise_s else round(step, 4)
        self.interval_s = round((pts[-1][0] - pts[0][0]) / (len(pts) - 1), 2)
        if vote > 0:
            top = envelope_top(ys)
            gap = statistics.mean(top - y for y in ys if y <= top)
            self.lag_depth_s = round(0.6 * self.lag_depth_s + 0.4 * gap, 3) if self.lag_depth_s else round(gap, 3)
        if vote:
            self.votes = max(-2 * PROFILE_VOTES, min(2 * PROFILE_VOTES, self.votes + vote))
        kind = ("lagging" if self.votes >= PROFILE_VOTES else
                "symmetric" if self.votes <= -PROFILE_VOTES else self.kind)
        self.kind = kind
        # worth saving: a new kind, or a value that really moved
        if kind != before[0] or abs(self.noise_s - before[1]) > 0.002 \
                or abs(self.lag_depth_s - before[2]) > 0.01:
            self.changed = True


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
    media_type: str = "video"
    # time lock (sync.time_lock, on by default): once the ghost sits on the target,
    # the timeline stops taking per-report corrections and just runs at 1.0x;
    # reports are still measured against it and release it when they disagree
    locked: bool = False
    lock_release_s: float = 0.02
    lock_residual: float | None = None     # median report-vs-locked-timeline gap
    steady_reports: int = 0                # quiet reports since the last event
    residual: float | None = None          # recent report-vs-timeline gap (before correcting)
    noise: float = 0.0                     # spread (MAD) of recent residuals: this client's jitter
    precise: bool = False                  # reports are precisely timed (SessionWatcher.precise_timing)
    # the player's learned report profile, and the steady stretch it is read
    # from: (report_mono, raw), raw = report - a fixed 1.0x line from the
    # stretch's first report. Events start a new stretch.
    profile: ReportProfile | None = None
    raw: deque = field(default_factory=deque)
    raw_ref: tuple[float, float] | None = None     # (pos, mono) the 1.0x line starts at
    since_look: int = 0
    env_prior: float | None = None         # lag_depth assumed until the first real tooth top
    deep: int = 0                          # reports in a row far under the envelope
    last_lift: float = 0.0
    env_gap: float | None = None           # latest report under the envelope (lagging)

    @property
    def envelope(self) -> bool:
        """Steer by the top of the reports: a player known to lag."""
        return self.profile is not None and self.profile.kind == "lagging"

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

    def lock(self, release_s: float) -> None:
        self.locked = True
        self.lock_release_s = float(release_s)
        self.lock_residual = None
        self.residuals.clear()

    def unlock(self) -> None:
        self.locked = False
        self.lock_residual = None
        self.steady_reports = 0
        self.residual = None

    def settled(self, reports: int, window_s: float) -> bool:
        """Quiet for ``reports`` reports and the client's recent reports agree
        with the timeline to within ``window_s`` - or within its own jitter,
        for a client whose reports are noisier than that: the sync is at 0."""
        return (self.stall_kind is None and not self.paused and self.steady_reports >= reports
                and self.residual is not None and abs(self.residual) <= max(window_s, 3.0 * self.jitter))

    @property
    def jitter(self) -> float:
        """This client's report scatter: what the recent reports show, or - for
        a player learned to scatter both ways - what it is known to do. The
        recent figure starts from nothing after every lock; Jellyfin for
        Android (one timed report per 10 s, +/-0.14 s) released the lock on
        its first noisy report every time."""
        known = self.profile.noise_s if (self.profile and self.profile.kind == "symmetric") else 0.0
        return max(self.noise, known)

    def begin_stall(self, kind: str, pos: float, report_mono: float) -> None:
        self._anchor(pos, report_mono + self.stalls.predict(kind))
        self.stall_kind = kind
        self.stall_pos = float(pos)
        self.stall_report_mono = report_mono
        self.residuals.clear()
        self.new_stretch()
        self.unlock()

    def new_stretch(self) -> None:
        """A seek, pause, resume or re-buffer: the reports start a new line."""
        self.raw.clear()
        self.raw_ref = None
        self.since_look = 0
        self.deep = 0
        self.env_prior = None

    def _take_raw(self, r: Report, report_mono: float) -> float:
        if self.raw_ref is None:
            self.raw_ref = (r.pos, report_mono)
            if self.envelope and self.profile.lag_depth_s > 0:
                # a fresh line starts somewhere on the sawtooth: assume the
                # average depth until a tooth top shows where the audio is
                self.env_prior = self.profile.lag_depth_s
        y = r.pos - self.raw_ref[0] - (report_mono - self.raw_ref[1])
        self.raw.append((report_mono, y))
        while self.raw and report_mono - self.raw[0][0] > ENVELOPE_WINDOW_S:
            self.raw.popleft()
        if self.env_prior is not None and len(self.raw) >= 3 and \
                (min(y, self.raw[-2][1]) - self.raw[-3][1] > BIG_STEP_S
                 or report_mono - self.raw[0][0] >= ENVELOPE_WINDOW_S - 1):
            self.env_prior = None     # a real tooth top: the envelope speaks for itself now
        if self.profile is not None:
            self.since_look += 1
            # by count, or - for a player with few timed reports (Jellyfin for
            # Android: one per 10 s) - by time
            if self.since_look >= CLASSIFY_EVERY or (
                    self.since_look >= 8 and report_mono - self.raw[0][0] >= CLASSIFY_SLOW_SPAN_S):
                self.since_look = 0
                self.profile.look(list(self.raw))
        return y

    def _follow_envelope(self, r: Report, report_mono: float, pred: float, y: float) -> str | None:
        """A lagging player: the timeline is a 1.0x line through the top of
        its reports. Reports under it are the player's lag, not news - only a
        tooth top moves it (``lift``), and a lock survives that."""
        top = envelope_top([v for _, v in self.raw])
        if self.env_prior is not None:
            top = max(top, self.env_prior)
        gap = y - top
        self.env_gap = gap
        noise = self.profile.noise_s if self.profile else 0.0
        self.noise = noise
        if gap < -max(LAG_TOL_S, 4.0 * noise):
            self.deep += 1
            if self.deep >= LAG_TOL_REPORTS:
                # far under, and staying there: the audio really fell behind
                # (a re-buffer the player did not report) - start over from it
                self._anchor(r.pos, report_mono)
                self.new_stretch()
                self.unlock()
                return "resync"
            return None
        self.deep = 0
        line = self.raw_ref[0] + (report_mono - self.raw_ref[1]) + top
        lift = line - pred
        self.steady_reports += 1
        self.residual = lift
        self._anchor(line, report_mono)
        if self.locked:
            self.lock_residual = gap
        if abs(lift) >= LIFT_LOG_S and len(self.raw) > 1:   # (the first point only places the line)
            self.last_lift = lift
            return "lift"
        return None


    def apply(self, r: Report, report_mono: float, jitter_tol: float, timed: bool = True) -> str | None:
        """Fold a (new) report in. Returns an event name or None.

        ``timed=False``: the report's time is only known to within a poll (see
        ``SessionWatcher._report_time``) - enough to spot a seek or a pause,
        not to steer the timeline by."""
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
            self.new_stretch()
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
            elif not timed:
                # Phase-locked to our polls (1 s reports, 0.5 s polls) the
                # timing error of such reports does not average out: it wanders
                # +/-0.25 s over tens of seconds, which is what kept releasing
                # the time lock live. Only reports Jellyfin timed steer.
                pass
            elif self.envelope:
                y = self._take_raw(r, report_mono)
                event = self._follow_envelope(r, report_mono, pred, y)
                self._debug(r, pred, delta, event)
                return event
            else:
                self._take_raw(r, report_mono)
                # steady state: the rate is exactly 1.0, only the intercept is
                # uncertain - correct it slowly from the median residual so
                # per-report position jitter does not reach the ghost
                self.steady_reports += 1
                self.residuals.append(delta)
                med = statistics.median(self.residuals)
                if len(self.residuals) >= 3:
                    self.noise = statistics.median(abs(x - med) for x in self.residuals)
                # precisely timed, consistent reports (Moonfin: ~4 ms) steer
                # hard - the last three, in full; noisy ones (CAMusic: ~0.1 s)
                # keep the slow median filter
                sharp = self.precise and self.noise < SHARP_NOISE_S
                recent = statistics.median(list(self.residuals)[-3:]) if sharp else med
                self.residual = recent
                if self.locked:
                    # locked: the timeline stands; the median residual is only
                    # watched, and a persistent gap (a short re-buffer, clock
                    # skew over a long film) hands back to the corrections
                    self.lock_residual = med
                    # a gap within the client's own jitter is not a gap, and
                    # a small one needs three reports to agree on it (one
                    # noisy report is not a re-buffer)
                    tol = max(self.lock_release_s, 3.0 * self.jitter)
                    if abs(med) <= tol or (len(self.residuals) < 3 and abs(med) <= 4.0 * tol):
                        self._debug(r, pred, delta, None)
                        return None
                    self.unlock()
                    self.lock_residual = med      # what released it, for the log
                    event = "unlock"
                if sharp:
                    corr = max(-RESIDUAL_MAX_STEP, min(RESIDUAL_MAX_STEP, recent))
                else:
                    corr = max(-RESIDUAL_MAX_STEP, min(RESIDUAL_MAX_STEP, med)) * RESIDUAL_GAIN
                self._anchor(pred + corr, report_mono)
                self.residuals = deque((x - corr for x in self.residuals), maxlen=RESIDUAL_WINDOW)
        if event and event not in ("unlock", "lift"):
            self.unlock()
        self._debug(r, pred, delta, event)
        return event

    def _debug(self, r: Report, pred: float, delta: float, event: str | None) -> None:
        self.last_debug = ("report #%d pos=%.2f%s pred=%.2f delta=%+.2f%s%s stall=%s" % (
            self.reports, r.pos, " paused" if r.paused else "", pred, delta,
            (" -> " + event) if event else "",
            " locked (median %+.2f)" % self.lock_residual if self.locked and self.lock_residual is not None else "",
            {k: round(v, 1) for k, v in self.stalls.est.items()}))


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
        # time lock also times reports properly: see _report_time
        self.precise_timing = False
        self.profiles: dict[str, ReportProfile] = {}   # per device id, learned (sync.player_profiles)
        self._last_checkin: float | None = None
        self._timed = True             # whether the last report's time is known precisely

    # -- pure core --------------------------------------------------------
    def observe(self, sessions: list[dict], server_epoch: float | None,
                local_epoch: float, now_mono: float) -> Observation:
        self.clock.add(server_epoch, local_epoch)
        s, who = self.players.pick(sessions, now_mono)
        prev_poll = self._prev_poll_mono
        self._prev_poll_mono = now_mono
        was_playing = self._playing_last_poll
        self._playing_last_poll = False
        if s is None:
            self.model = None
            return Observation(False, False, None, None, None)
        r = report_from_session(s)
        if r is None or (who is not None and not who.wants(r)):
            # connected, but playing nothing we follow: a film on a player bound
            # to music only, or an album on one bound to films only
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
                last_key=r.key, reports=1, last_report_mono=report_mono, device_id=r.device_id,
                media_type=r.media_type)
            # playback that just appeared (not: the daemon started mid-movie)
            # is a fresh start: the client reports its position, then buffers
            just_started = prev_poll is not None and not was_playing and (now_mono - report_mono) < 3.0
            self.model.profile = self.profile_for(r.device_id)
            if just_started and not r.paused:
                self.model.begin_stall("start", r.pos, report_mono)
            if self.model.envelope and not r.paused:
                # a lagging player's first report of a song is somewhere on its
                # sawtooth: open where its audio is on average, not on the report
                self.model.anchor_pos += self.model.profile.lag_depth_s
            self.model.last_debug = "report #1 pos=%.2f age=%.2fs%s" % (
                r.pos, now_mono - report_mono, " (start stall %.1fs)" % self.stalls.predict("start") if just_started else "")
            return Observation(True, True, self.model, "new_item", r, stale, player=who)
        self.model.precise = self.precise_timing
        event = self.model.apply(r, report_mono, self.jitter_tol, timed=self._timed)
        return Observation(True, True, self.model, event, r, False, player=who)

    def _report_time(self, r: Report, local_epoch: float, now_mono: float,
                     prev_poll: float | None, first: bool) -> tuple[float, bool]:
        """Local monotonic time at which the client took this report."""
        fallback = now_mono if first else now_mono - self.poll_interval / 2.0
        stale_checkin = not first and r.checkin is not None and r.checkin == self._last_checkin
        self._last_checkin = r.checkin
        self._timed = not (stale_checkin and self.precise_timing)
        if r.checkin is None:
            return fallback, False
        if stale_checkin and self.precise_timing:
            # Moonfin sends its position every second, but Jellyfin moves
            # LastPlaybackCheckIn only every ~5 s. A new position under an old
            # check-in was not timed by it: it arrived somewhere since the last
            # poll, so the middle of that window is the unbiased guess. (Aging
            # it from the stale check-in clamps it to the previous poll, which
            # put the model ~0.2 s ahead of the TV on live data.)
            lo = prev_poll if prev_poll is not None else now_mono - self.poll_interval
            return now_mono - max(0.0, now_mono - lo) / 2.0, False
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

    def profile_for(self, device_id: str | None) -> ReportProfile | None:
        if not device_id:
            return None
        p = self.profiles.get(device_id)
        if p is None:
            p = self.profiles[device_id] = ReportProfile()
        return p

    def load_profiles(self, saved) -> None:
        if isinstance(saved, dict):
            for dev, d in saved.items():
                if isinstance(dev, str) and dev:
                    self.profiles[dev] = ReportProfile.from_dict(d)

    def profiles_changed(self) -> bool:
        return any(p.changed for p in self.profiles.values())

    def dump_profiles(self) -> dict:
        for p in self.profiles.values():
            p.changed = False
        return {dev: p.to_dict() for dev, p in self.profiles.items() if p.kind != "unknown" or p.noise_s}

    # -- I/O wrapper ------------------------------------------------------
    def poll(self, client: JellyfinClient) -> Observation:
        sessions, server_epoch, local_epoch = client.sessions()
        return self.observe(sessions, server_epoch, local_epoch, time.monotonic())

    def reset(self) -> None:
        self.model = None
