"""Music sync (2.12): learn how each player's reports err, follow the real
audio, correct by seeks and never by tempo, land song starts at once.

The numbers come from a 15-minute live recording of CAMusic-linux on
2026-10-19: its reports lost 2.44 ms/s against Jellyfin's clock and jumped
+0.22 s every 70-100 s (MPD's ``elapsed`` lagging the audio, then catching up)."""
import random
import statistics

import pytest

from hueghost import daemon as dm
from hueghost.lockstep import GhostObs, Params, Seek, Speed, decide
from hueghost.watcher import ReportProfile, SessionWatcher, SessionMatcher
from tests.test_daemon_flow import MONO0, RunningGhost, _music, world  # noqa: F401  (fixture)
from tests.test_watcher import DEV, Sim, errs, lock_when_settled, steps


def sawtooth(period=90.0, slope=0.00244, phase=30.0):
    """CAMusic-linux: the report falls behind the audio at ``slope`` s/s, then
    catches up in one step every ``period`` seconds."""
    return lambda t: -slope * ((t + phase) % period)


def music_watcher(profiles=None):
    w = SessionWatcher(SessionMatcher(device_id=DEV), jitter_tolerance_s=1.5, poll_interval_s=0.5)
    w.precise_timing = True
    if profiles:
        w.load_profiles(profiles)
    return w


def test_a_sawtooth_player_is_learned_as_lagging_and_the_lock_holds():
    sim = Sim(report_every=1.0, seed=11, stall_start=0.0)
    sim.report_error = sawtooth()
    w = music_watcher()
    res = sim.run(w, 900.0, hook=lock_when_settled)
    p = w.profiles[DEV]
    assert p.kind == "lagging", p
    assert 0.05 < p.lag_depth_s < 0.2 and p.noise_s < 0.01
    # once learned, the timeline follows the top of the reports - the audio -
    # and the lock is never handed back
    learned = next(t for (t, _, ev) in res if ev == "lift")
    assert not [t for (t, _, ev) in res if ev == "unlock" and t > learned]
    e = errs(res, learned + 100, 900)
    assert statistics.mean(e) < 0.02 and max(e) < 0.05, (statistics.mean(e), max(e))


def test_the_old_filter_chased_the_sawtooth():
    """What 2.11 did with the same player: the lock released over and over and
    the estimate wandered with the reports (the 'in sync, then out' effect)."""
    sim = Sim(report_every=1.0, seed=11, stall_start=0.0)
    sim.report_error = sawtooth()
    w = music_watcher({DEV: {"kind": "symmetric"}})
    res = sim.run(w, 900.0, hook=lock_when_settled)
    assert sum(1 for (_, _, ev) in res if ev == "unlock") >= 5


def test_a_learned_profile_is_used_from_the_first_report():
    sim = Sim(report_every=1.0, seed=12, stall_start=0.0)
    sim.report_error = sawtooth()
    w = music_watcher({DEV: {"kind": "lagging", "noise_s": 0.003, "lag_depth_s": 0.11}})
    res = sim.run(w, 300.0, hook=lock_when_settled)
    assert not any(ev == "unlock" for (t, _, ev) in res if t > 15)
    # the song opens with the average depth added: within ~half a tooth at
    # once (the first seconds are the clock offset settling, as for any player)
    assert max(errs(res, 15, 60)) < 0.12
    assert statistics.mean(errs(res, 150, 300)) < 0.02


def test_a_noisy_symmetric_player_is_not_taken_for_a_lagging_one():
    rng = random.Random(3)
    sim = Sim(report_every=1.0, seed=13, stall_start=0.0)
    sim.report_error = lambda t: rng.gauss(0.0, 0.05)
    w = music_watcher()
    sim.run(w, 600.0, hook=lock_when_settled)
    assert w.profiles[DEV].kind != "lagging"


def test_a_clean_player_is_learned_as_symmetric():
    sim = Sim(report_every=1.0, seed=14, stall_start=0.0)
    w = music_watcher()
    sim.run(w, 400.0, hook=lock_when_settled)
    assert w.profiles[DEV].kind == "symmetric"


def test_a_real_seek_and_a_rebuffer_still_count_on_a_lagging_player():
    sim = Sim(report_every=1.0, seed=15, stall_start=0.0, stall_seek=0.5)
    sim.report_error = sawtooth()
    sim.events += [(200.0, "seek", 1500.0), (400.0, "hiccup", 0.8)]
    w = music_watcher({DEV: {"kind": "lagging", "noise_s": 0.003, "lag_depth_s": 0.11}})
    res = sim.run(w, 600.0, hook=lock_when_settled)
    evs = [(t, ev) for (t, _, ev) in res if ev in ("seek", "resync")]
    assert any(200 < t < 203 and ev == "seek" for t, ev in evs), evs
    assert any(400 < t < 406 and ev == "resync" for t, ev in evs), evs
    assert statistics.mean(errs(res, 550, 600)) < 0.05


def test_profiles_round_trip_and_ignore_rubbish():
    w = music_watcher({DEV: {"kind": "lagging", "noise_s": 0.0025, "lag_depth_s": 0.093},
                       "x": {"kind": "wibble", "noise_s": "a"}, "": {}})
    assert w.profiles[DEV].kind == "lagging" and w.profiles["x"].kind == "unknown"
    w.profiles[DEV].changed = True
    assert w.profiles_changed()
    dump = w.dump_profiles()
    assert dump[DEV] == {"kind": "lagging", "noise_s": 0.0025, "lag_depth_s": 0.093, "interval_s": 0.0}
    assert "x" not in dump and not w.profiles_changed()
    assert ReportProfile.from_dict(None).kind == "unknown"


def _replay_live(w):
    """Feed the recorded CAMusic-linux reports to a watcher at 4 Hz polls."""
    import json
    import os
    from hueghost.jellyfin import TICKS_PER_S
    data = json.load(open(os.path.join(os.path.dirname(__file__), "data", "camusic_sawtooth.json")))
    from hueghost.jellyfin import parse_jf_datetime
    # "seen" is when the 4 Hz recorder noticed a report - up to a poll late.
    # A report reaches /Sessions at its check-in; rebuild that on our clock
    # from the smallest seen-minus-check-in (the network floor, ~10 ms).
    reps = data["reports"]
    floor = min(seen - parse_jf_datetime(ci) for seen, _, ci, _, _ in reps)
    reps = [[parse_jf_datetime(ci) + floor, pos, ci, item, rt] for _, pos, ci, item, rt in reps]
    events, locked = [], []
    i, t = 0, 0.0
    while t <= reps[-1][0]:
        # an even poll grid, as the daemon polls: what /Sessions showed then
        while i + 1 < len(reps) and reps[i + 1][0] <= t:
            i += 1
        _, pos, checkin, item, rt = reps[i]
        s = {"DeviceId": DEV, "DeviceName": "Linux", "Client": "Camusic linux",
             "LastPlaybackCheckIn": checkin,
             "NowPlayingItem": {"Id": "song%d" % item, "Name": "Song", "MediaType": "Audio",
                                "RunTimeTicks": int(rt * TICKS_PER_S)},
             "PlayState": {"PositionTicks": int(round(pos * TICKS_PER_S)), "IsPaused": False}}
        obs = w.observe([s], None, 1_760_000_000.0 + t, MONO0 + t)
        if obs.event:
            events.append((t, obs.event))
        lock_when_settled(obs)
        locked.append(obs.model.locked)
        t += 0.25
    return events, locked


def test_the_live_recording_replays_without_chasing_the_sawtooth():
    w = music_watcher()
    events, locked = _replay_live(w)
    # the player is still learned while the songs are held
    assert w.profiles[DEV].kind == "lagging"
    first = next(i for i, x in enumerate(locked) if x)
    assert first * 0.25 < 20, "locked within the first seconds of the song"
    # held for the whole song: nothing but the next song moves it
    assert [ev for t, ev in events if ev not in ("new_item",)] == [], events
    assert sum(locked) / len(locked) > 0.95


# -- the lockstep: music never touches the tempo -------------------------------------

def _ghost(pos, speed=1.0, last_seek=float("-inf")):
    return GhostObs(pos=pos, paused=False, buffering=False, speed=speed, last_seek_mono=last_seek)


def test_seek_only_lockstep_never_changes_speed():
    p = Params(speed_nudge=False, deadband_s=0.04, seek_threshold_s=0.04, seek_cooldown_s=2.0)
    assert decide(100.0, False, _ghost(100.02), p, 10.0) == []
    assert decide(100.0, False, _ghost(100.2), p, 10.0) == [Seek(100.0)]
    assert decide(100.0, False, _ghost(99.9), p, 10.0) == [Seek(100.0)]
    # within the cooldown: wait, and never nudge meanwhile
    assert decide(100.0, False, _ghost(100.2, last_seek=9.0), p, 10.0) == []
    # a speed left over from a video ghost is put back
    assert decide(100.0, False, _ghost(100.0, speed=1.03), p, 10.0) == [Speed(1.0)]
    # the default still nudges, as before
    assert decide(100.0, False, _ghost(100.2), Params(), 10.0) == [Speed(0.96)]


# -- the daemon: a music ghost --------------------------------------------------------

class LateSwapGhost(RunningGhost):
    """A music ghost whose loadfile takes ``latency`` s to play: it reaches
    the position it was opened at that much later than it was asked to."""

    latency = 0.8           # the prior is 0.5: the first swaps land behind

    def swap(self, url, start=None):
        super().swap(url, start)
        self._time_pos, self._t0 = (start or 0.0) - self.latency, self.clock()
        return True


@pytest.fixture
def music(world, monkeypatch):  # noqa: F811
    world.d.cfg.set("ghost.audio_device", "{0.0.0.00000000}.{silent}")

    def launch(cfg_, url, hdr, start, item_id, err_path=None, **kw):
        g = LateSwapGhost(item_id, lambda: MONO0 + world.t, start + 0.3)
        world.launched.append(g)
        return g
    monkeypatch.setattr(dm.GhostPlayer, "launch", staticmethod(launch))
    return world


def _song(world, pos0, seconds, item="track1", t0=None):
    t0 = world.t if t0 is None else t0
    end = world.t + seconds
    while world.t < end - 1e-9:
        world.step(1.0, _music(pos0 + (world.t - t0), world.t, item=item))
    return t0


def test_music_ghost_corrects_by_seeks_only_and_locks(music):
    d = music.d
    _song(music, 30.0, 40.0)
    g = music.ghost
    assert d._ghost_audio_only
    assert not [a for a in g.actions if isinstance(a, Speed) and a.value != 1.0], "no tempo change, ever"
    assert any(isinstance(a, Seek) for a in g.actions)
    assert d.last_obs.model.locked
    m = d.last_obs.model
    assert abs(g.pos - m.position_at(MONO0 + music.t)) <= 0.04


def test_a_new_song_lands_at_once_and_the_swap_latency_is_learned(music):
    d = music.d
    _song(music, 30.0, 20.0)
    g = music.ghost
    misses = []
    for k in range(8):
        n = len(g.actions)
        t0 = _song(music, 0.0, 1.0, item="track%d" % (k + 2))
        seeks = [a for a in g.actions[n:] if isinstance(a, Seek)]
        if k == 0:
            assert seeks, "a song that opens behind gets its seek at the first position"
        _song(music, 0.0, 10.0, item="track%d" % (k + 2), t0=t0)
        m = d.last_obs.model
        assert abs(g.pos - m.position_at(MONO0 + music.t)) <= 0.05
        assert not [a for a in g.actions[n:] if isinstance(a, Speed) and a.value != 1.0]
        misses.append(len(seeks))
    # learned: later songs open on target and need no seek at all
    assert abs(d._swap_latency - LateSwapGhost.latency) < 0.1, d._swap_latency
    assert misses[-1] == 0, misses


def test_a_paused_song_is_aligned_once_not_on_every_tick(music):
    """Live (2026-10-19): with a learned seek lead, every alignment seek of a
    paused music ghost landed past the target, and it seeked again each tick
    until the pause limit stopped it - 60 seeks in 15 s."""
    d = music.d
    d._seek_lead = 0.057
    t0 = _song(music, 30.0, 20.0)
    g = music.ghost
    n = g.seeks
    pos = 30.0 + (music.t - t0)
    for _ in range(10):
        music.step(1.0, _music(pos, music.t, paused=True))
    assert g.paused and g.seeks - n <= 1, g.seeks - n


def test_music_offset_applies_to_music_only(music):
    d = music.d
    d.cfg.set("sync.offset_s", -0.1)
    assert d.offset == -0.1                     # nothing playing: the film offset
    d.cfg.set("sync.music_offset_s", 0.15)
    _song(music, 30.0, 3.0)
    assert d.offset == 0.15
    d.cfg.set("sync.music_offset_s", None)
    assert d.offset == -0.1                     # None: music shares the film offset


def test_a_sparse_noisy_player_is_learned_and_holds_the_lock():
    """Jellyfin for Android (live, 2026-10-19): a position every second but a
    timed check-in only every 10 s, scattered +/-0.14 s both ways. It is
    learned as symmetric within a few minutes, and once known its own scatter
    sets the lock's tolerance - one noisy report no longer releases it."""
    rng = random.Random(21)
    sim = Sim(report_every=1.0, checkin_every=10.0, seed=21, stall_start=0.0)
    sim.report_error = lambda t: rng.uniform(-0.14, 0.14)
    w = music_watcher()
    res = sim.run(w, 1200.0, hook=lock_when_settled)
    p = w.profiles[DEV]
    assert p.kind == "symmetric", p
    known = next(t for (t, _, _) in res if t > 400)
    unlocks = [t for (t, _, ev) in res if ev == "unlock" and t > known]
    assert len(unlocks) <= 3, unlocks
    assert statistics.mean(errs(res, 600, 1200)) < 0.08


# -- music: lock once per song, then leave it ------------------------------------------

def _music_sim(seed, **kw):
    sim = Sim(report_every=1.0, seed=seed, stall_start=0.0, **kw)
    sim.media = "Audio"
    return sim


@pytest.mark.parametrize("player", ["camusic", "phone"])
def test_a_locked_song_is_left_alone_until_the_player_does_something(player):
    """Abdullah (2026-10-19): once a song is locked and confirmed, leave it -
    both players run at the same speed. Steering by report scatter made the
    lights look like they were chasing the song."""
    rng = random.Random(31)
    if player == "camusic":
        sim = _music_sim(31)
        sim.report_error = sawtooth()
        w = music_watcher({DEV: {"kind": "lagging", "noise_s": 0.003, "lag_depth_s": 0.11}})
    else:
        sim = _music_sim(31, checkin_every=10.0)
        sim.report_error = lambda t: rng.uniform(-0.14, 0.14)
        w = music_watcher({DEV: {"kind": "symmetric", "noise_s": 0.09}})
    locked_at = []
    steps_after = []

    def hook(obs):
        m = obs.model
        was = m.locked
        lock_when_settled(obs, settle=3 if player == "camusic" else 6)
        if m.locked and not was and not locked_at:
            locked_at.append(m.position_at(MONO0))
    res = sim.run(w, 900.0, hook=hook)
    assert w.model.locked
    after = [(t, ev) for (t, _, ev) in res if ev and t > 120]
    assert after == [], after                    # no unlock, no lift, nothing
    # held, the timeline runs at exactly 1.0x: no steps at all
    assert max(steps(res, 120)) < 0.005


def test_a_locked_song_still_follows_seeks_pauses_and_rebuffers():
    sim = _music_sim(32, stall_seek=0.5)
    sim.events += [(200.0, "seek", 1500.0), (300.0, "pause", 0), (310.0, "resume", 0),
                   (400.0, "hiccup", 0.8)]
    w = music_watcher({DEV: {"kind": "symmetric", "noise_s": 0.004}})
    res = sim.run(w, 500.0, hook=lock_when_settled)
    evs = [(round(t), ev) for (t, _, ev) in res if ev in ("seek", "pause", "resume", "resync")]
    kinds = [ev for _, ev in evs]
    assert kinds[:3] == ["seek", "pause", "resume"] and "resync" in kinds, evs
    assert any(400 < t < 405 for t, ev in evs if ev == "resync"), evs
    assert w.model.locked and statistics.mean(errs(res, 450, 500)) < 0.03
