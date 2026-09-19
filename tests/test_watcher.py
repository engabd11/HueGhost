"""Simulates a TV client that reports progress only every N seconds and checks
that the position model tracks the truth closely from 0.5 s polls."""
import math
import random

from hueghost.jellyfin import TICKS_PER_S
from hueghost.watcher import ClockOffset, SessionMatcher, SessionWatcher, report_from_session

DEV = "atv-device-id"
LOCAL0 = 1_700_000_000.0   # local epoch at t=0
MONO0 = 5_000.0


def iso(epoch: float) -> str:
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%07d" % (dt.microsecond * 10) + "Z"


def session(pos: float, paused: bool, checkin_epoch: float | None, item="item1"):
    return {
        "DeviceId": DEV, "DeviceName": "Living Room", "Client": "Swiftfin", "UserName": "abd",
        "LastPlaybackCheckIn": iso(checkin_epoch) if checkin_epoch else "0001-01-01T00:00:00.0000000Z",
        "NowPlayingItem": {"Id": item, "Name": "Ep", "MediaType": "Video", "Type": "Episode",
                           "SeriesName": "Show", "ParentIndexNumber": 1, "IndexNumber": 2,
                           "RunTimeTicks": int(3600 * TICKS_PER_S)},
        "PlayState": {"PositionTicks": int(round(pos * TICKS_PER_S)), "IsPaused": paused,
                      "MediaSourceId": "ms1"},
    }


class Sim:
    """Ground truth playback + a client that reports every `report_every` s."""

    def __init__(self, server_offset=37.3, report_every=10.0, poll=0.5, seed=1, report_latency=0.03):
        self.off, self.every, self.poll, self.lat = server_offset, report_every, poll, report_latency
        self.rng = random.Random(seed)
        self.events: list[tuple[float, str, float]] = []   # (t, kind, value)
        self.paused_since = None
        self.pos0 = 600.0

    def truth(self, t: float) -> tuple[float, bool]:
        pos, paused, last_t = self.pos0, False, 0.0
        for (et, kind, val) in self.events:
            if et > t:
                break
            if not paused:
                pos += et - last_t
            last_t = et
            if kind == "seek":
                pos = val
            elif kind == "pause":
                paused = True
            elif kind == "resume":
                paused = False
        if not paused:
            pos += t - last_t
        return pos, paused

    def report_times(self, t: float) -> list[float]:
        """Client reports on a cadence plus immediately on events."""
        ts = [k * self.every + 0.2 for k in range(int(t // self.every) + 1)]
        ts += [et + 0.05 for (et, _, _) in self.events]
        return sorted(x for x in ts if x <= t)

    def run(self, watcher: SessionWatcher, duration: float):
        t = 0.0
        out = []
        while t < duration:
            rts = self.report_times(t)
            rt = rts[-1] if rts else None
            if rt is None:
                t += self.poll
                continue
            pos_r, paused_r = self.truth(rt)
            checkin = rt + self.lat + LOCAL0 + self.off
            sess = session(pos_r, paused_r, checkin)
            server_now = math.floor(t + LOCAL0 + self.off)      # Date header: whole seconds
            obs = watcher.observe([sess], server_now, t + LOCAL0, t + MONO0)
            est = obs.model.position_at(t + MONO0)
            tp, _ = self.truth(t)
            out.append((t, est - tp, obs.event))
            t += self.poll + self.rng.uniform(0.0, 0.08)        # realistic poll jitter
        return out


def make_watcher(poll=0.5):
    return SessionWatcher(SessionMatcher(device_id=DEV), jitter_tolerance_s=0.75, poll_interval_s=poll)


def test_steady_playback_tracks_truth_closely():
    sim = Sim()
    res = sim.run(make_watcher(), 180.0)
    errs = [abs(e) for (t, e, ev) in res if t > 40]     # after the estimator has settled
    assert max(errs) < 0.15, max(errs)
    assert not any(ev == "seek" for (_, _, ev) in res), "no spurious seeks from stale reports"


def test_first_anchor_corrects_report_staleness():
    sim = Sim()
    # daemon starts 7 s after the last report
    w = make_watcher()
    t = 47.0
    rt = 40.2
    pos_r, _ = sim.truth(rt)
    obs = w.observe([session(pos_r, False, rt + LOCAL0 + sim.off)], math.floor(t + LOCAL0 + sim.off), t + LOCAL0, t + MONO0)
    est = obs.model.position_at(t + MONO0)
    truth, _ = sim.truth(t)
    assert abs(est - truth) < 1.0           # within the Date header's 1 s resolution
    assert est > pos_r + 5.0                # not the 7 s stale value


def test_real_seek_detected_within_one_poll():
    sim = Sim()
    sim.events.append((60.0, "seek", 1200.0))
    res = sim.run(make_watcher(), 90.0)
    seeks = [t for (t, e, ev) in res if ev == "seek"]
    assert len(seeks) == 1
    assert 60.0 <= seeks[0] <= 60.7
    after = [abs(e) for (t, e, ev) in res if t > 61.0]
    assert max(after) < 0.15


def test_pause_freezes_and_resume_realigns():
    sim = Sim()
    sim.events.append((30.0, "pause", 0))
    sim.events.append((45.0, "resume", 0))
    res = sim.run(make_watcher(), 80.0)
    kinds = [ev for (_, _, ev) in res if ev in ("pause", "resume")]
    assert kinds == ["pause", "resume"]
    frozen = [abs(e) for (t, e, ev) in res if 31 < t < 44]
    assert max(frozen) < 0.2
    after = [abs(e) for (t, e, ev) in res if t > 47]
    assert max(after) < 0.15


def test_clients_without_checkin_still_track():
    sim = Sim()
    w = make_watcher()
    # strip the check-in timestamps: model falls back to poll-based anchoring
    t = 0.5
    errs = []
    while t < 60:
        rts = sim.report_times(t)
        pos_r, _ = sim.truth(rts[-1])
        obs = w.observe([session(pos_r, False, None)], None, t + LOCAL0, t + MONO0)
        tp, _ = sim.truth(t)
        errs.append(abs(obs.model.position_at(t + MONO0) - tp))
        t += 0.5
    assert max(errs[4:]) < 1.0


def test_clock_offset_is_monotone_max():
    c = ClockOffset()
    c.add(100.3, 100.0)
    c.add(100.1, 101.0)      # lower sample must not pull the estimate down
    assert abs(c.value - 0.3) < 1e-9
    c.add(102.45, 102.0)
    assert abs(c.value - 0.45) < 1e-9


def test_matcher_prefers_playing_session_and_device_id():
    m = SessionMatcher(device_id=DEV)
    idle = {"DeviceId": DEV, "DeviceName": "x", "Client": "y"}
    playing = session(1.0, False, None)
    assert m.pick([idle, playing]) is playing
    assert SessionMatcher(name_contains="living").pick([playing]) is playing
    assert SessionMatcher(name_contains="kitchen").pick([playing]) is None
    assert SessionMatcher(device_id="other").pick([playing]) is None


def test_report_from_session_shapes():
    r = report_from_session(session(12.5, True, LOCAL0))
    assert r.pos == 12.5 and r.paused and r.media_source_id == "ms1"
    assert r.name == "Show - S01E02 - Ep"
    assert report_from_session({"NowPlayingItem": {"MediaType": "Audio"}}) is None
