"""An app playing on this PC.

No ghost: the picture is already on a screen the Hue Sync app can capture, so
the work is pointing it at the right display rather than reproducing anything.
Music is the exception in the other direction - it has no picture, so the app
is put in music mode and listens to the PC's own output.
"""
from __future__ import annotations

import logging

from ..engines import AUTO, Plan
from ..pcwatch import PcProbe
from . import Activity, Source

log = logging.getLogger("hue-ghost.sources")


class PcSource(Source):
    id = "pc"

    def __init__(self, cfg, bindings: list[dict], probe: PcProbe | None = None):
        super().__init__()
        self.cfg = cfg
        self._bindings = list(bindings)
        self.interval_s = float(cfg.get("pc.poll_interval_s", 1.0))
        self.probe = probe or PcProbe(
            peak=float(cfg.get("pc.audio_peak", 0.002)),
            hold_s=float(cfg.get("pc.audio_hold_s", 3.0)),
            confirm_s=float(cfg.get("pc.start_confirm_s", 1.0)))
        self._was: dict[str, bool] = {}

    def bindings(self) -> list[dict]:
        return list(self._bindings)

    def ignore_pid(self, pid: int | None) -> None:
        """Our own ghost must never be detected as content - someone who binds
        mpv.exe would otherwise sync to the ghost we launched ourselves."""
        self.probe.ignore_pids = {int(pid)} if pid else set()

    def poll(self, now: float) -> list[Activity]:
        return self.activities(self.probe.poll(self._bindings, now))

    def activities(self, hits: dict) -> list[Activity]:
        """Pure: the probe's reading becomes one Activity per binding."""
        out = []
        for b in self._bindings:
            hit = hits.get(b["id"])
            if hit is None:
                continue
            was = self._was.get(b["id"], False)
            self._was[b["id"]] = hit.playing
            mode = b.get("mode") or "video"
            out.append(Activity(
                binding=b,
                seen=bool(hit.running),
                playing=bool(hit.playing),
                kind="game" if mode == "games" else mode,
                needs_ghost=False,
                plan=self._plan(b, hit),
                ghost=None,
                obs=None,
                event="new_item" if (hit.playing and not was) else None,
                item_key="%s:%s" % (b["exe"], mode),
                title=hit.title or b.get("name") or b["exe"]))
        return out

    def _plan(self, b: dict, hit) -> Plan:
        mode = b.get("mode") or "video"
        monitor = None
        if mode != "music":
            # follow the window: a game moved to the second screen should light
            # from that screen, not from wherever it was bound
            monitor = b.get("monitor") or hit.monitor_id or _primary_monitor()
        return Plan(
            area_id=b.get("area_id") or None,
            mode=mode,
            monitor=monitor or None,
            # the PC's own output is what music mode should listen to here, and
            # it has to be able to undo an endpoint a Jellyfin-music session pinned
            audio_device=(b.get("audio_device") or AUTO) if mode == "music" else None,
            use_audio=self.cfg.get("engine.huesync.use_audio"),
            # the binding's own level, else the global one
            intensity=(b.get("intensity")
                       or self.cfg.get("engine.huesync.intensity") or None))


def _primary_monitor() -> str:
    from ..winutil import list_displays

    for d in list_displays():
        if d.primary:
            return d.monitor_id
    return ""
