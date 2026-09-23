"""Simulates a TV client that reports progress only every N seconds, sits on a
still frame for a few seconds after every seek / start / resume (buffering,
as the Apple TV does), and optionally reports whole-second positions - and
checks that the position model tracks the truth closely from 0.5 s polls."""
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
        "DeviceId": DEV, "DeviceName": "Living Room", "Client": "Moonfin for tvOS", "UserName": "abd",
        "LastPlaybackCheckIn": iso(checkin_epoch) if checkin_epoch else "0001-01-01T00:00:00.0000000Z",
        "NowPlayingItem": {"Id": item, "Name": "Ep", "MediaType": "Video", "Type": "Episode",
                           "SeriesName": "Show", "ParentIndexNumber": 1, "IndexNumber": 2,
                           "RunTimeTicks": int(3600 * TICKS_PER_S)},
        "PlayState": {"PositionTicks": int(round(pos * TICKS_PER_S)), "IsPaused": paused,
                      "MediaSourceId": "ms1"},
    }


class Sim:
    """Ground truth playback + a client that reports every `report_every` s and
    stalls (still frame, position not advancing) after seek / start / resume."""

    def __init__(self, server_offset=37.3, report_every=10.0, poll=0.5, seed=1, report_latency=0.03,
                 stall_seek=4.0, stall_start=3.5, stall_resume=0.8, quantize=False):
        self.off, self.every, self.poll, self.lat = server_offset, report_every, poll, report_latency
        self.stall_seek, self.stall_start, self.stall_resume = stall_seek, stall_start, stall_resume
        self.quantize = quantize
        self.rng = random.Random(seed)
        self.events: list[tuple[float, str, float]] = []   # (t, kind, value)
        self.pos0 = 600.0
        self.t_start = 0.2   # the client sends its first (start) report here

    def truth(self, t: float) -> tuple[float, bool]:
        pos, paused, last_t = self.pos0, False, self.t_start
        frozen_until = self.t_start + self.stall_start

        def advance(a, b):
            nonlocal pos
            a2 = max(a, frozen_until)
            if b > a2 and not paused:
                pos += b - a2

        for (et, kind, val) in self.events:
            if et > t:
                break
            advance(last_t, et)
            last_t = et
            if kind == "seek":
                pos = val
                frozen_until = et + self.stall_seek
            elif kind == "pause":
                paused = True
            elif kind == "resume":
                paused = False
                frozen_until = et + self.stall_resume
        advance(last_t, t)
        return pos, paused

    def report_times(self, t: float) -> list[float]:
        """Client reports on a cadence plus immediately on events."""
        ts = [k * self.every + self.t_start for k in range(int(t // self.every) + 1)]
        ts += [et + 0.05 for (et, _, _) in self.events]
        return sorted(x for x in ts if self.t_start <= x <= t)

    def run(self, watcher: SessionWatcher, duration: float):
        t = 0.0
        out = []
        while t < duration:
            rts = self.report_times(t)
            rt = rts[-1] if rts else None
            server_now = math.floor(t + LOCAL0 + self.off)      # Date header: whole seconds
            if rt is None:
                watcher.observe([], server_now, t + LOCAL0, t + MONO0)   # idle poll before playback
                t += self.poll
                continue
            pos_r, paused_r = self.truth(rt)
            if self.quantize:
                pos_r = math.floor(pos_r)
            checkin = rt + self.lat + LOCAL0 + self.off
            obs = watcher.observe([session(pos_r, paused_r, checkin)], server_now, t + LOCAL0, t + MONO0)
            est = obs.model.position_at(t + MONO0)
            tp, _ = self.truth(t)
            out.append((t, est - tp, obs.event))
            t += self.poll + self.rng.uniform(0.0, 0.08)        # realistic poll jitter
        return out


def make_watcher(poll=0.5):
    return SessionWatcher(SessionMatcher(device_id=DEV), jitter_tolerance_s=1.5, poll_interval_s=poll)


def errs(res, lo, hi):
    return [abs(e) for (t, e, ev) in res if lo < t < hi]


def test_steady_playback_tracks_truth_closely():
    sim = Sim()
    res = sim.run(make_watcher(), 180.0)
    assert max(errs(res, 40, 180)) < 0.15
    assert not any(ev in ("seek", "resync") for (_, _, ev) in res), "no spurious seeks from stale reports"


def test_start_stall_is_applied_when_playback_appears():
    sim = Sim(stall_start=3.5)
    res = sim.run(make_watcher(), 30.0)
    # during the client's initial buffering the model must hold, not run ahead
    assert max(errs(res, 0.6, 3.5)) < 0.3
    assert max(errs(res, 12, 30)) < 0.15


def test_first_anchor_corrects_report_staleness_when_daemon_starts_mid_movie():
    sim = Sim()
    w = make_watcher()
    t = 47.0
    rt = 40.2
    pos_r, _ = sim.truth(rt)
    obs = w.observe([session(pos_r, False, rt + LOCAL0 + sim.off)], math.floor(t + LOCAL0 + sim.off), t + LOCAL0, t + MONO0)
    est = obs.model.position_at(t + MONO0)
    truth, _ = sim.truth(t)
    assert abs(est - truth) < 1.0           # within the Date header's 1 s resolution
    assert est > pos_r + 5.0                # not the 7 s stale value
    assert not obs.model.frozen(t + MONO0)  # no start stall: we joined mid-playback


def test_real_seek_detected_within_one_poll_and_stall_held():
    sim = Sim(stall_seek=4.0)
    sim.events.append((60.0, "seek", 1200.0))
    res = sim.run(make_watcher(), 90.0)
    seeks = [t for (t, e, ev) in res if ev == "seek"]
    assert len(seeks) == 1 and 60.0 <= seeks[0] <= 60.7
    # prior says 3.5 s, the client takes 4.0 s: never more than that mismatch off
    assert max(errs(res, 61, 70)) < 0.6
    # the first advancing report (70.2) re-anchors exactly
    assert max(errs(res, 71, 90)) < 0.15


def test_stall_learning_converges_over_seeks():
    sim = Sim(stall_seek=6.0)                  # far from the 3.5 s prior
    for k, t in enumerate((60.0, 120.0, 180.0, 240.0)):
        sim.events.append((t, "seek", 1000.0 + 300 * k))
    w = make_watcher()
    res = sim.run(w, 300.0)
    windows = [max(errs(res, t + 1, t + 10)) for t in (60.0, 120.0, 180.0, 240.0)]
    assert windows[0] > windows[-1] + 0.5, windows      # learning shrinks the transient
    assert windows[-1] < 0.8
    assert abs(w.stalls.predict("seek") - 6.0) < 0.6
    assert max(errs(res, 251, 300)) < 0.15


def test_pause_freezes_and_resume_realigns():
    sim = Sim(stall_resume=0.8)
    sim.events.append((30.0, "pause", 0))
    sim.events.append((45.0, "resume", 0))
    res = sim.run(make_watcher(), 80.0)
    kinds = [ev for (_, _, ev) in res if ev in ("pause", "resume")]
    assert kinds == ["pause", "resume"]
    assert max(errs(res, 31, 44)) < 0.2
    assert max(errs(res, 45.2, 50)) < 0.3    # resume prior 1.0 vs 0.8 actual
    assert max(errs(res, 52, 80)) < 0.15


def test_whole_second_reports_are_smoothed_not_chased():
    sim = Sim(quantize=True)
    res = sim.run(make_watcher(), 240.0)
    e = errs(res, 60, 240)
    # floor() quantisation is a constant ~0.5 s bias (absorbed by sync.offset_s);
    # what matters is that it is steady, not chased report by report
    assert max(e) < 0.8 and max(e) - min(e) < 0.3
    assert not any(ev in ("seek", "resync") for (_, _, ev) in res)
    # the model must not jitter: consecutive estimates move smoothly
    steps = [abs(res[i][1] - res[i - 1][1]) for i in range(1, len(res)) if res[i][0] > 60]
    assert max(steps) < 0.2


def test_clients_without_checkin_still_track():
    sim = Sim(stall_start=0.0)
    w = make_watcher()
    t = 0.5
    errors = []
    while t < 60:
        rts = sim.report_times(t)
        pos_r, _ = sim.truth(rts[-1])
        obs = w.observe([session(pos_r, False, None)], None, t + LOCAL0, t + MONO0)
        tp, _ = sim.truth(t)
        errors.append(abs(obs.model.position_at(t + MONO0) - tp))
        t += 0.5
    assert max(errors[12:]) < 1.0


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


def _phone(client, user, device_id, playing=True):
    s = session(10.0, False, LOCAL0 + 5)
    s["DeviceId"], s["DeviceName"], s["Client"], s["UserName"] = device_id, "Android", client, user
    if not playing:
        s.pop("NowPlayingItem")
        s["PlayState"] = {}
    return s


def test_one_phone_can_hold_a_separate_config_per_app():
    """The whole point of matching on the app: Jellyfin gives a phone's music
    app and its video app different device ids, so each can drive its own
    lights - and neither should answer for the other."""
    music = SessionMatcher(device_id="id-music", client="CAMusic")
    video = SessionMatcher(device_id="id-video", client="Jellyfin for Android")
    s_music = _phone("CAMusic", "Abdullah", "id-music")
    s_video = _phone("Jellyfin for Android", "Abdullah", "id-video")
    assert music.matches(s_music) and not music.matches(s_video)
    assert video.matches(s_video) and not video.matches(s_music)


def test_matching_by_name_can_be_narrowed_to_one_app():
    """'Android' alone follows every Android in the house; adding the app - and
    the user - is what makes a name match safe on a phone."""
    loose = SessionMatcher(name_contains="Android")
    narrow = SessionMatcher(name_contains="Android", client="CAMusic", user="Mariam")
    mine = _phone("CAMusic", "Abdullah", "id-1")
    hers = _phone("CAMusic", "Mariam", "id-2")
    other_app = _phone("Jellyfin for Android", "Mariam", "id-3")
    assert loose.matches(mine) and loose.matches(hers)          # the mix-up
    assert narrow.matches(hers)
    assert not narrow.matches(mine) and not narrow.matches(other_app)
