"""Sources: whatever can tell us that something is playing.

``Source`` is the mirror of ``engines.Engine`` - the engine end decides what
turns a picture into light, this end decides where the picture comes from.
Two exist: a Jellyfin client on the network (mirrored by the ghost, because the
picture is on a TV we cannot capture) and an app on this PC (not mirrored - the
picture is already on a screen Hue Sync can capture).

Only one of them can be live at a time: the Hue Sync app has one entertainment
area, one capture display and one mode. So ``SourceSet`` picks a winner by the
order the user put the bindings in.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..engines import Plan
from ..watcher import Observation

log = logging.getLogger("hue-ghost.sources")


@dataclass(frozen=True)
class GhostSpec:
    """What the ghost player should open. ``audio_only`` skips the display
    entirely: music has no picture to mirror, only sound to reproduce."""
    url: str
    http_header: str = ""
    item_id: str = ""
    audio_only: bool = False
    audio_device: str = ""


IDLE_PLAN = Plan()


@dataclass
class Activity:
    """What one binding says is happening, now.

    ``obs`` is the Jellyfin position model and is None for a PC source - there
    is no position to model when the thing is already on screen. Nothing
    downstream asks for one, because ``needs_ghost`` is False in that case.
    """
    binding: dict | None = None
    seen: bool = False
    playing: bool = False
    kind: str = "video"                   # video | music | game
    needs_ghost: bool = False
    plan: Plan = IDLE_PLAN
    ghost: GhostSpec | None = None
    obs: Observation | None = None
    event: str | None = None
    item_key: str = ""                    # identity of what is playing
    title: str = ""

    @property
    def area_id(self) -> str | None:
        return self.plan.area_id


IDLE = Activity()


class Source:
    """Duck-typed, like ``Engine``. ``poll`` must not raise: a source that
    cannot reach its server reports it and keeps the last activity, so one
    failed request does not tear a running ghost down."""
    id = "none"
    interval_s = 0.5

    def __init__(self) -> None:
        self.ok = True
        self.error: str | None = None
        self.last = IDLE
        self._next_poll = 0.0

    def poll(self, now: float) -> Activity:
        return IDLE

    def bindings(self) -> list[dict]:
        return []

    def close(self) -> None:
        pass


class SourceSet:
    """Every source, polled at its own rate, with one winner.

    The winner is the first *playing* binding in the user's order. A binding
    that is merely present (a connected client, a running app) only matters
    when nothing is playing, so the UI can say "seen, idle"."""

    def __init__(self, sources: list[Source], hold_s: float = 20.0):
        self.sources = sources
        self.hold_s = float(hold_s)
        self._held: str | None = None      # binding id currently driving the lights
        self._held_since = 0.0

    def close(self) -> None:
        for s in self.sources:
            try:
                s.close()
            except Exception:
                log.debug("closing source %s failed", s.id, exc_info=True)

    def bindings(self) -> list[dict]:
        return [b for s in self.sources for b in s.bindings()]

    def poll(self, now: float) -> Activity:
        acts: list[Activity] = []
        for s in self.sources:
            if now >= s._next_poll:
                try:
                    s.last = s.poll(now)
                    s.ok, s.error = True, None
                except Exception as e:           # a source must never kill the loop
                    s.ok, s.error = False, str(e)
                    log.debug("source %s failed: %s", s.id, e, exc_info=True)
                s._next_poll = now + s.interval_s
            acts.extend(s.last if isinstance(s.last, list) else [s.last])
        # the user's order across *all* bindings, not the order of the sources:
        # a game listed above the TV beats the TV
        acts.sort(key=_rank)
        playing = [a for a in acts if a.playing]
        if not playing:
            self._held = None
            return next((a for a in acts if a.seen), IDLE)
        win = playing[0]
        # Switching binding restarts the Hue Sync app (a different area or
        # capture display), so a TV and a game alternating must not do it every
        # few seconds: the one that got there first keeps it for a while.
        if self._held and self._held != _bid(win) and now - self._held_since < self.hold_s:
            held = next((a for a in playing if _bid(a) == self._held), None)
            if held is not None:
                return held
        if self._held != _bid(win):
            self._held, self._held_since = _bid(win), now
        return win


def _bid(a: Activity) -> str:
    return (a.binding or {}).get("id", "")


def _rank(a: Activity) -> int:
    return (a.binding or {}).get("rank", 1 << 30)


def build_sources(cfg, jf_client=None) -> SourceSet:
    """One source per kind, each owning the bindings that belong to it."""
    from .jellyfin import JellyfinSource
    from .pc import PcSource

    binds, skipped = [], []
    for i, b in enumerate(cfg.enabled_bindings()):
        probs = cfg.binding_problems(b)
        if probs:
            skipped.append("%s (%s)" % (b["name"] or b["id"], "; ".join(probs)))
            continue
        binds.append(dict(b, rank=i))      # list order is priority order
    for s in skipped:
        log.warning("binding not usable, skipped: %s", s)

    sources: list[Source] = []
    jelly = [b for b in binds if b["source"] == "jellyfin"]
    if jelly:
        sources.append(JellyfinSource(cfg, jelly, client=jf_client))
    pc = [b for b in binds if b["source"] == "pc"]
    if pc:
        sources.append(PcSource(cfg, pc))
    return SourceSet(sources, hold_s=float(cfg.get("engine.huesync.min_session_s", 20.0)))
