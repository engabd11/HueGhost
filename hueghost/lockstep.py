"""Pure lockstep policy: given where the followed client is and where the ghost
really is, decide what to tell mpv. No I/O, no clocks - fully unit-testable.

drift = ghost_pos - target_pos   (positive: ghost is ahead)

  |drift| >= seek_threshold  (and cooldown passed, or the target just seeked)
        -> hard seek to target
  deadband < |drift| < seek_threshold
        -> proportional speed: 1 - clamp(drift / converge_s, +/- max_speed_delta)
           (a 0.2 s lead with converge_s=5 -> speed 0.96, gone in ~5 s, invisible)
  |drift| <= deadband
        -> speed 1.0
While the ghost is buffering nothing is corrected (its clock is not running).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Seek:
    pos: float


@dataclass(frozen=True)
class Speed:
    value: float


@dataclass(frozen=True)
class Pause:
    pass


@dataclass(frozen=True)
class Resume:
    pass


Action = Seek | Speed | Pause | Resume


@dataclass
class GhostObs:
    pos: float | None          # real position (None until mpv reported one)
    paused: bool
    buffering: bool
    speed: float
    last_seek_mono: float      # -inf if never


@dataclass
class Params:
    seek_threshold_s: float = 1.0
    deadband_s: float = 0.05
    converge_s: float = 5.0
    max_speed_delta: float = 0.04
    seek_cooldown_s: float = 3.0

    @classmethod
    def from_config(cls, cfg) -> "Params":
        s = cfg.section("sync")
        return cls(
            seek_threshold_s=float(s.get("seek_threshold_s", 1.0)),
            deadband_s=float(s.get("deadband_s", 0.05)),
            converge_s=float(s.get("converge_s", 5.0)),
            max_speed_delta=float(s.get("max_speed_delta", 0.04)),
            seek_cooldown_s=float(s.get("seek_cooldown_s", 3.0)),
        )


def speed_for(drift: float, p: Params) -> float:
    if abs(drift) <= p.deadband_s:
        return 1.0
    delta = drift / max(p.converge_s, 0.1)
    delta = max(-p.max_speed_delta, min(p.max_speed_delta, delta))
    return round(1.0 - delta, 4)


def decide(target_pos: float, target_paused: bool, ghost: GhostObs, p: Params,
           now_mono: float, force_seek: bool = False) -> list[Action]:
    actions: list[Action] = []

    if target_paused:
        if not ghost.paused:
            actions.append(Pause())
        if ghost.pos is not None and abs(ghost.pos - target_pos) >= p.seek_threshold_s:
            actions.append(Seek(target_pos))      # align while paused so resume is clean
        if abs(ghost.speed - 1.0) > 1e-6:
            actions.append(Speed(1.0))
        return actions

    if ghost.paused:
        # resume: land exactly on target, then play
        actions.append(Seek(target_pos))
        actions.append(Resume())
        if abs(ghost.speed - 1.0) > 1e-6:
            actions.append(Speed(1.0))
        return actions

    if ghost.pos is None or ghost.buffering:
        if abs(ghost.speed - 1.0) > 1e-6:
            actions.append(Speed(1.0))
        return actions

    drift = ghost.pos - target_pos
    cooldown_ok = (now_mono - ghost.last_seek_mono) >= p.seek_cooldown_s
    if abs(drift) >= p.seek_threshold_s and (cooldown_ok or force_seek):
        if abs(ghost.speed - 1.0) > 1e-6:
            actions.append(Speed(1.0))
        actions.append(Seek(target_pos))
        return actions
    if force_seek and abs(drift) > p.deadband_s:
        actions.append(Seek(target_pos))
        if abs(ghost.speed - 1.0) > 1e-6:
            actions.append(Speed(1.0))
        return actions

    want = speed_for(drift, p)
    if abs(want - ghost.speed) > 0.0015:
        actions.append(Speed(want))
    return actions
