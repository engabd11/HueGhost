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
