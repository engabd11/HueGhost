"""Screen care: no static picture on an OLED while Hue Ghost holds it awake.

Hue Sync needs the display it captures to stay on, so while a ghost plays Hue
Ghost stops Windows switching the displays off - *every* display, because that
is the only switch Windows has. On an OLED that means hours of one unchanging
picture: the desktop on the screens nobody is looking at, and the frame the
ghost is paused on. Two cheap remedies:

- **pixel shift** - every few minutes the ghost's picture moves a few pixels,
  round a slow orbit, so no pixel shows the same thing all night. Hue Sync
  averages large zones of the screen; the lights cannot tell.
- **black out** - after some minutes with no mouse or keyboard, every display
  Hue Sync is *not* capturing is covered in black (a black OLED pixel is off).
  Any input takes it away again, like a screensaver. The display the lights
  come from is never covered.

Both only run while a video ghost is up. An app playing on this PC is left
alone: that picture is yours, on a screen you are looking at.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Callable

from .winutil import Display, idle_seconds, kill_on_exit, list_displays

log = logging.getLogger("hue-ghost.screen")
INPUT_CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cover_input.conf")

# A slow orbit round the true position; every point is visited equally often,
# and the orbit carries on across ghosts so a night of short episodes does not
# keep wearing the first few positions.
ORBIT = ((0, 0), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))
# mpv's video-pan unit is the scaled picture itself: ~6 px across a 1080p
# picture, ~12 px across a 4K one. Invisible, and far below the size of the
# zones Hue Sync averages over.
SHIFT_STEP = 0.003


def orbit_offset(step: int) -> tuple[float, float]:
    """The picture's (video-pan-x, video-pan-y) for one step of the orbit."""
    dx, dy = ORBIT[step % len(ORBIT)]
    return round(dx * SHIFT_STEP, 4), round(dy * SHIFT_STEP, 4)


def ghost_display(cfg, displays: list[Display]) -> Display | None:
    """The display the ghost plays on, resolved the way ``ghost.resolve_screen``
    resolves it for mpv: by name, else by index. None when neither is set -
    mpv then picks a display itself, and nobody knows which."""
    name = str(cfg.get("ghost.screen_name") or "").lower()
    if name:
        hit = next((d for d in displays if d.name.lower() == name), None)
        if hit is not None:
            return hit
    idx = cfg.get("ghost.screen_index")
    if idx is not None and str(idx) != "":
        return next((d for d in displays if d.index == int(idx)), None)
    return None


def cover_args(cfg, index: int) -> list[str]:
    """A borderless, always-on-top black window over one whole display.

    mpv already knows how to put a window on display N - the same index the
    ghost uses - so it is the blanker too: no second windowing toolkit, and the
    headless daemon gets it exactly like the app does. It never takes focus
    and hides the cursor; a click (or Esc) closes it, the one way out should
    Hue Ghost not be there to take it away."""
    return [cfg.get("ghost.mpv_path") or "mpv", "--no-config", "--no-border", "--ontop",
            "--force-window=yes", "--screen=%d" % index, "--fs-screen=%d" % index, "--fullscreen=yes",
            "--osd-level=0", "--osc=no", "--cursor-autohide=always", "--focus-on=never",
            "--input-default-bindings=no", "--input-conf=" + INPUT_CONF,
            "--hwdec=no", "--aid=no", "--keep-open=yes", "--msg-level=all=no",
            "--title=hue-ghost screen care",
            "av://lavfi:color=c=black:s=64x64:r=1"]


def _minutes(cfg, key: str, default: float) -> float:
    try:
        return max(0.0, float(cfg.get(key, default) or 0.0))
    except (TypeError, ValueError):
        return default


class ScreenCare:
    """Ticked by the daemon loop, under its lock, like the rest of the ghost."""

    def __init__(self, cfg,
                 idle: Callable[[], float | None] = idle_seconds,
                 displays: Callable[[], list[Display]] = list_displays,
                 launch: Callable[[list[str]], object] | None = None):
        self.cfg = cfg
        self._idle = idle
        self._displays = displays
        self._launch = launch or _launch
        # pixel shift
        self._ghost = None
        self._step = 0
        self._next_shift: float | None = None
        self.pan = (0.0, 0.0)
        # black out: display name -> the mpv covering it
        self.covers: dict[str, object] = {}
        self._covered = False       # this idle stretch has been handled; wait for input

    @property
    def shift_every_s(self) -> float:
        return _minutes(self.cfg, "ghost.pixel_shift_min", 3.0) * 60.0

    @property
    def blackout_after_s(self) -> float:
        return _minutes(self.cfg, "ghost.blackout_idle_min", 0.0) * 60.0

    def tick(self, now: float, ghost, spare: set[str] | frozenset = frozenset()) -> None:
        """``ghost`` is the live *video* ghost (None when there is none, or it
        is music). ``spare`` holds monitor DeviceIDs never to cover besides the
        ghost's own display: the one the Hue Sync app says it captures."""
        self._shift(now, ghost)
        self._blackout(ghost is not None, spare)

    # -- pixel shift -------------------------------------------------------------
    def _shift(self, now: float, ghost) -> None:
        every = self.shift_every_s
        if ghost is None:
            self._ghost, self._next_shift, self.pan = None, None, (0.0, 0.0)
            return
        if ghost is not self._ghost:            # a fresh mpv starts centred
            self._ghost, self.pan = ghost, (0.0, 0.0)
            self._next_shift = now + every if every else None
            if every:
                self._pan_to(ghost, orbit_offset(self._step))
            return
        if not every:
            self._next_shift = None
            self._pan_to(ghost, (0.0, 0.0))     # switched off mid-film: back to centre
            return
        if self._next_shift is None or self._next_shift - now > every:
            self._next_shift = now + every      # just switched on, or made more frequent
        if now >= self._next_shift:
            self._step += 1
            self._next_shift = now + every
            self._pan_to(ghost, orbit_offset(self._step))

    def _pan_to(self, ghost, pan: tuple[float, float]) -> None:
        if pan == self.pan:
            return
        try:
            ghost.set_pan(*pan)
        except Exception as e:                  # a ghost on its way out; the next starts afresh
            log.debug("pixel shift not applied: %s", e)
            return
        self.pan = pan
        log.debug("pixel shift: ghost picture at (%+.4f, %+.4f)", *pan)

    # -- black out ---------------------------------------------------------------
    def _blackout(self, video_ghost: bool, spare) -> None:
        after = self.blackout_after_s
        idle = self._idle() if (video_ghost and after) else None
        if idle is None or idle < after:
            if self.covers:
                log.info("screen care: %s -> other displays back",
                         "ghost closed" if not video_ghost else "input again" if after else "switched off")
            self.uncover()
            self._covered = False
            return
        if self._covered:
            return                              # covered already (or nothing to cover) until input
        self._covered = True
        displays = self._displays()
        ghost = ghost_display(self.cfg, displays)
        if ghost is None:
            log.warning("screen care: the ghost display is not set (Display page), so there is no "
                        "telling which screen Hue Sync needs - not blacking anything out")
            return
        targets = [d for d in displays
                   if d.name != ghost.name and not (d.monitor_id and d.monitor_id in spare)]
        if not targets:
            return
        for d in targets:
            try:
                self.covers[d.name] = self._launch(cover_args(self.cfg, d.index))
            except Exception as e:
                log.warning("screen care: could not black out %s: %s", d.name, e)
        if self.covers:
            log.info("screen care: no mouse or keyboard for %.0f min -> blacked out %s "
                     "(the lights keep %s)", after / 60.0, ", ".join(self.covers), ghost.name)

    def uncover(self) -> None:
        for p in self.covers.values():
            try:
                p.terminate()
                p.wait(timeout=1.0)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self.covers = {}

    def close(self) -> None:
        self.uncover()

    def cover_pids(self) -> list[int]:
        """The black covers' processes: fullscreen windows of our own that
        must never be mistaken for something playing on this PC."""
        return [p.pid for p in self.covers.values() if getattr(p, "pid", None)]

    def status(self) -> dict:
        return {
            "pixel_shift_min": self.shift_every_s / 60.0,
            "blackout_idle_min": self.blackout_after_s / 60.0,
            "pan": list(self.pan),
            "blacked_out": sorted(self.covers),
        }


def _launch(args: list[str]) -> subprocess.Popen:
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=creation)
    # a black window over someone's monitor must never outlive Hue Ghost, even a crashed one
    kill_on_exit(p)
    return p
