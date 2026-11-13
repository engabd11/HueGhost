from hueghost.lockstep import GhostObs, Params, Pause, Resume, Seek, Speed, decide, speed_for

P = Params(seek_threshold_s=1.0, deadband_s=0.05, converge_s=5.0, max_speed_delta=0.04, seek_cooldown_s=3.0)


def obs(pos, paused=False, buffering=False, speed=1.0, last_seek=-1e9):
    return GhostObs(pos=pos, paused=paused, buffering=buffering, speed=speed, last_seek_mono=last_seek)


def test_deadband_holds_speed_one():
    assert decide(100.0, False, obs(100.03), P, 10.0) == []
    assert decide(100.0, False, obs(99.96), P, 10.0) == []


def test_proportional_nudge_direction_and_clamp():
    # ghost 0.2 s ahead -> slow down by 0.2/5 = 0.04
    assert decide(100.0, False, obs(100.2), P, 10.0) == [Speed(0.96)]
    # ghost 0.1 s behind -> speed up by 0.02
    assert decide(100.0, False, obs(99.9), P, 10.0) == [Speed(1.02)]
    # clamp: 0.9 s ahead would be 0.18 -> clamped to 0.04
    assert decide(100.0, False, obs(100.9), P, 10.0) == [Speed(0.96)]
    assert speed_for(-0.9, P) == 1.04


def test_nudge_converges_without_overshoot():
    ghost, target, speed = 100.3, 100.0, 1.0
    t = 0.0
    dt = 0.25
    for _ in range(200):
        acts = decide(target, False, obs(ghost, speed=speed), P, t)
        for a in acts:
            if isinstance(a, Speed):
                speed = a.value
        ghost += dt * speed
        target += dt
        t += dt
    assert abs(ghost - target) < 0.05
    assert ghost - target > -0.02        # never crossed far below the target


def test_speed_returns_to_one_inside_deadband():
    assert decide(100.0, False, obs(100.02, speed=0.96), P, 10.0) == [Speed(1.0)]


def test_seek_beyond_threshold_respects_cooldown():
    assert decide(100.0, False, obs(102.0), P, 10.0) == [Seek(100.0)]
    # a seek 1 s ago -> cooldown blocks; falls back to (clamped) nudge
    assert decide(100.0, False, obs(102.0, last_seek=9.0), P, 10.0) == [Speed(0.96)]
    # force (client seeked) overrides cooldown
    assert decide(100.0, False, obs(102.0, last_seek=9.0), P, 10.0, force_seek=True) == [Seek(100.0)]


def test_force_seek_small_drift_still_seeks_but_not_inside_deadband():
    assert decide(100.0, False, obs(100.4), P, 10.0, force_seek=True) == [Seek(100.0)]
    assert decide(100.0, False, obs(100.02), P, 10.0, force_seek=True) == []


def test_seek_resets_speed_first():
    acts = decide(100.0, False, obs(102.0, speed=0.96), P, 10.0)
    assert acts == [Speed(1.0), Seek(100.0)]


def test_pause_and_resume_propagation():
    assert decide(100.0, True, obs(100.0), P, 10.0) == [Pause()]
    assert decide(100.0, True, obs(100.0, paused=True), P, 10.0) == []
    # paused far away -> align while paused
    assert decide(100.0, True, obs(105.0, paused=True), P, 10.0) == [Seek(100.0)]
    # resume lands exactly on target then plays
    assert decide(100.0, False, obs(100.4, paused=True), P, 10.0) == [Seek(100.0), Resume()]


def test_buffering_or_unknown_position_holds():
    assert decide(100.0, False, obs(103.0, buffering=True), P, 10.0) == []
    assert decide(100.0, False, obs(103.0, buffering=True, speed=1.04), P, 10.0) == [Speed(1.0)]
    assert decide(100.0, False, obs(None), P, 10.0) == []


def test_the_locked_hold_pulls_a_few_milliseconds_back_with_an_invisible_trim():
    """Live 2026-11-13: locked, the ghost sat 20 ms off the timeline for half an
    hour - the 50 ms deadband never looked at it."""
    from hueghost.daemon import FINE_CONVERGE_S, FINE_DEADBAND_S, FINE_MAX_SPEED_DELTA, FINE_SPEED_STEP

    fine = Params(deadband_s=FINE_DEADBAND_S, converge_s=FINE_CONVERGE_S,
                  max_speed_delta=FINE_MAX_SPEED_DELTA, speed_step=FINE_SPEED_STEP)
    [s] = decide(100.0, False, obs(100.02), fine, 10.0)
    assert 0.997 <= s.value < 1.0
    assert decide(100.0, False, obs(100.002), fine, 10.0) == []              # inside the hold
    assert decide(100.0, False, obs(100.02, speed=s.value + 0.0001), fine, 10.0) == []   # not worth sending
    # it converges: simulate 30 s of 0.25 s ticks
    pos, speed = 100.02, 1.0
    for _ in range(120):
        for a in decide(100.0, False, obs(pos, speed=speed), fine, 10.0):
            speed = a.value
        pos += (speed - 1.0) * 0.25
    assert abs(pos - 100.0) <= FINE_DEADBAND_S


def _settle(params, start, seconds=40.0, noise=0.002, seed=3):
    """Closed loop at the daemon's 0.25 s tick: the ghost starts ``start`` off
    the timeline and is steered by decide(); readings carry ``noise`` (the
    filtered position, live: ~2 ms). Returns the drift trace."""
    import random
    rnd = random.Random(seed)
    drift, speed, out = start, 1.0, []
    for _ in range(int(seconds / 0.25)):
        seen = drift + rnd.uniform(-noise, noise)
        for a in decide(100.0, False, obs(100.0 + seen, speed=speed), params, 10.0):
            if isinstance(a, Speed):
                speed = a.value
        drift += (speed - 1.0) * 0.25
        out.append(drift)
    return out


def test_armed_settles_a_seek_in_seconds_without_overshoot():
    """Live 2.14.0: a seek's 0.1 s timeline step took 23-26 s back to 0.00 -
    a 5 s converge stopping at 15 ms, then the slow fine hold."""
    from dataclasses import replace

    from hueghost.daemon import ARMED_CONVERGE_S, ARMED_DEADBAND_S, LOCK_WINDOW_S

    base = Params()
    armed = replace(base, deadband_s=ARMED_DEADBAND_S, converge_s=ARMED_CONVERGE_S)
    before = replace(base, deadband_s=LOCK_WINDOW_S * 0.75)
    for start in (-0.17, -0.1, 0.1):
        trace = _settle(armed, start)
        first = next(i for i, d in enumerate(trace) if abs(d) < 0.005) * 0.25
        assert first <= 8.0, (start, first)
        assert all(abs(d) < 0.006 for d in trace[int(first / 0.25):]), (start, "overshoot")
        assert all(d * start >= -0.006 for d in trace), (start, "crossed over")
    old = _settle(before, -0.1)
    assert abs(old[int(8.0 / 0.25)]) > 0.01, "the old setting was the slow one"
