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
                 stall_seek=4.0, stall_start=3.5, stall_resume=0.8, quantize=False, rate=1.0,
                 checkin_every=None, poll_jitter=0.08):
        self.off, self.every, self.poll, self.lat = server_offset, report_every, poll, report_latency
        self.stall_seek, self.stall_start, self.stall_resume = stall_seek, stall_start, stall_resume
        self.quantize = quantize
        self.rate = rate     # the client's clock vs ours (1.0002 = 200 ppm fast)
        # Jellyfin moves LastPlaybackCheckIn less often than the client reports
        # (Moonfin: position every 1 s, check-in every ~5 s)
        self.checkin_every = checkin_every
        self.poll_jitter = poll_jitter
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
                pos += (b - a2) * self.rate

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
            elif kind == "hiccup":
                # a short re-buffer the client does not report as an event
                frozen_until = max(frozen_until, et + val)
        advance(last_t, t)
        return pos, paused

    def report_times(self, t: float) -> list[float]:
        """Client reports on a cadence plus immediately on events."""
        ts = [k * self.every + self.t_start for k in range(int(t // self.every) + 1)]
        ts += [et + 0.05 for (et, kind, _) in self.events if kind != "hiccup"]
        return sorted(x for x in ts if self.t_start <= x <= t)

    def run(self, watcher: SessionWatcher, duration: float, hook=None):
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
            ct = rt
            if self.checkin_every:
                ct = self.t_start + math.floor((rt - self.t_start) / self.checkin_every + 1e-9) * self.checkin_every
            checkin = ct + self.lat + LOCAL0 + self.off
            obs = watcher.observe([session(pos_r, paused_r, checkin)], server_now, t + LOCAL0, t + MONO0)
            if hook:
                hook(obs)
            est = obs.model.position_at(t + MONO0)
            tp, _ = self.truth(t)
            out.append((t, est - tp, obs.event))
            t += self.poll + self.rng.uniform(0.0, self.poll_jitter)   # realistic poll jitter
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


# -- time lock (experimental) -------------------------------------------------------

def precise_watcher():
    w = make_watcher()
    w.precise_timing = True          # what sync.time_lock switches on
    return w


def lock_when_settled(obs, settle=3, agree=0.02, release=0.02):
    """What the daemon does once the ghost sits on the target: lock a model
    whose timed reports have agreed with it for a few reports."""
    m = obs.model
    if m is not None and not m.locked and m.settled(settle, agree):
        m.lock(release)


def steps(res, lo):
    return [abs(res[i][1] - res[i - 1][1]) for i in range(1, len(res)) if res[i][0] > lo]


def test_time_lock_holds_the_timeline_without_losing_accuracy():
    sim = Sim(report_every=1.0, seed=4)
    w = precise_watcher()
    res = sim.run(w, 300.0, hook=lock_when_settled)
    assert w.model.locked
    assert max(errs(res, 40, 300)) < 0.05
    # locked: the estimate runs at exactly 1.0x - nothing is re-anchored per report
    assert max(steps(res, 40)) < 0.02
    assert not any(ev in ("seek", "resync") for (_, _, ev) in res)


def test_stale_checkins_do_not_steer_the_timeline():
    """Moonfin reports its position every second but Jellyfin moves the
    check-in only every 5 s. Aged from the stale check-in, those reports are
    clamped to the previous poll and the model runs ahead of the TV (+0.2 s
    measured live). With precise timing only the timed reports steer."""
    def run(precise):
        sim = Sim(report_every=1.0, checkin_every=5.0, stall_start=0.0, seed=5)
        w = make_watcher()
        w.precise_timing = precise
        res = sim.run(w, 240.0)
        e = [x for (t, x, _) in res if t > 60]
        return sum(e) / len(e), max(abs(x) for x in e), sum(1 for x in steps(res, 60) if x > 0.01)
    old_bias, _, old_steps = run(False)
    new_bias, new_max, new_steps = run(True)
    assert old_bias > 0.1 and old_steps > 50, (old_bias, old_steps)   # the flaw (off = unchanged)
    assert abs(new_bias) < 0.05 and new_max < 0.1 and new_steps <= 2, (new_bias, new_max, new_steps)


def test_phase_locked_polls_do_not_make_the_lock_flap():
    """Found live: 1 s reports against exactly-0.5 s polls. Timing the untimed
    reports at the middle of the poll window is right on average, but here the
    error wanders +/-0.25 s over tens of seconds - the lock released every
    10-20 s. Untimed reports must not steer, nor count against the lock."""
    sim = Sim(report_every=1.0, checkin_every=5.0, stall_start=0.0, seed=9, poll_jitter=0.005)
    w = precise_watcher()
    res = sim.run(w, 900.0, hook=lock_when_settled)
    assert sum(1 for (_, _, ev) in res if ev == "unlock") <= 2
    assert w.model.locked
    assert sum(errs(res, 120, 900)) / len(errs(res, 120, 900)) < 0.03


def test_time_lock_releases_on_a_rebuffer_the_client_does_not_report():
    """A half-second re-buffer is below seek detection: no event arrives, only
    timed reports that disagree with the locked timeline."""
    sim = Sim(report_every=1.0, seed=6)
    sim.events.append((200.0, "hiccup", 0.5))
    w = precise_watcher()
    res = sim.run(w, 320.0, hook=lock_when_settled)
    # (a release in the first seconds is the clock offset still converging)
    unlocks = [t for (t, _, ev) in res if ev == "unlock" and t > 60]
    assert unlocks and 200.0 < unlocks[0] < 206.0, unlocks
    assert max(errs(res, 230, 320)) < 0.05       # corrected, then locked again
    assert w.model.locked


def test_time_lock_follows_clock_skew_by_releasing_and_relocking():
    sim = Sim(report_every=5.0, rate=1.0002, seed=7)       # a 200 ppm fast client
    w = precise_watcher()
    res = sim.run(w, 2400.0, hook=lock_when_settled)
    assert len([ev for (_, _, ev) in res if ev == "unlock"]) >= 2
    assert max(errs(res, 60, 2400)) < 0.1


def test_seek_pause_and_resume_release_the_lock():
    sim = Sim(report_every=1.0, seed=8)
    sim.events.append((60.0, "seek", 1500.0))
    w = precise_watcher()
    seen = []

    def hook(obs):
        if obs.event in ("seek", "pause", "resume"):
            seen.append((obs.event, obs.model.locked))
        lock_when_settled(obs)
    sim.run(w, 59.0, hook=hook)
    assert w.model.locked
    sim.events += [(80.0, "pause", 0), (90.0, "resume", 0)]
    sim.run(w, 100.0, hook=hook)
    assert [e for (e, _) in seen] == ["seek", "pause", "resume"]
    assert not any(locked for (_, locked) in seen), "every client event hands back to the corrections"


# -- which binding a session belongs to (2.10.1) --------------------------------------

def _sess(device_id, name, client, playing=True):
    s = session(10.0, False, LOCAL0 + 5)
    s["DeviceId"], s["DeviceName"], s["Client"] = device_id, name, client
    if not playing:
        s.pop("NowPlayingItem")
        s["PlayState"] = {}
    return s


def test_a_disabled_device_is_not_picked_up_by_another_binding_by_name():
    """Live 2.9.0: 'Android S23' (its own device id, plus the name "Android"
    from an older version) followed CAMusic-linux - a different device, also
    called "Android", whose own binding was switched off."""
    from hueghost.watcher import PlayerSet
    s23 = {"id": "s23", "device_id": "s4Zd", "device_name_contains": "Android"}
    linux_off = {"id": "linux", "device_id": "camusic-linux", "enabled": False}
    players = PlayerSet.from_players([s23], claimed={"s4Zd", "camusic-linux"})
    linux = _sess("camusic-linux", "Android", "Camusic linux")
    phone_idle = _sess("s4Zd", "Android", "CAMusic", playing=False)
    assert players.pick([phone_idle, linux]) == (phone_idle, players.matchers[0])
    assert players.pick([linux]) == (None, None), "claimed by its own (disabled) binding"


def test_the_name_still_finds_a_device_whose_id_was_regenerated():
    from hueghost.watcher import PlayerSet
    atv = {"id": "atv", "device_id": "old-id", "device_name_contains": "Apple TV"}
    players = PlayerSet.from_players([atv])
    moved = _sess("new-id", "Apple TV", "Moonfin")
    assert players.pick([moved])[0] is moved
    # ... but not while the device it was made for is itself connected
    assert players.pick([_sess("old-id", "Apple TV", "Moonfin", playing=False), moved])[0]["DeviceId"] == "old-id"


def test_name_only_bindings_never_take_a_claimed_device():
    from hueghost.watcher import PlayerSet
    by_name = {"id": "any-android", "device_name_contains": "Android"}
    players = PlayerSet.from_players([by_name], claimed={"camusic-linux"})
    assert players.pick([_sess("camusic-linux", "Android", "Camusic linux")]) == (None, None)
    other = _sess("pixel-1", "Android", "Jellyfin")
    assert players.pick([other])[0] is other
