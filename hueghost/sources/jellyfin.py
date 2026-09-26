"""A Jellyfin client on the network.

The picture is on a TV we cannot capture, so this source needs the ghost: the
same file, played here, in lockstep. All of the position modelling lives in
``watcher.py`` and is unchanged - this is the adapter between it and the daemon.
"""
from __future__ import annotations

import logging

from ..engines import Plan
from ..jellyfin import JellyfinClient, JellyfinError
from ..watcher import PlayerSet, SessionWatcher
from . import Activity, GhostSpec, Source

log = logging.getLogger("hue-ghost.sources")


class JellyfinSource(Source):
    id = "jellyfin"

    def __init__(self, cfg, bindings: list[dict], client: JellyfinClient | None = None):
        super().__init__()
        self.cfg = cfg
        self._bindings = list(bindings)
        self.interval_s = float(cfg.get("jellyfin.poll_interval_s", 0.5))
        self.client = client or JellyfinClient(cfg.get("jellyfin.url"), cfg.get("jellyfin.api_key"))
        self.watcher = self._make_watcher()
        self._warned_no_output = None

    def _make_watcher(self) -> SessionWatcher:
        # every Jellyfin binding's device, the switched-off ones too
        claimed = {b.get("device_id") for b in self.cfg.bindings()
                   if b.get("source") == "jellyfin" and b.get("device_id")}
        w = SessionWatcher(
            PlayerSet.from_players(self._bindings, claimed),
            jitter_tolerance_s=float(self.cfg.get("sync.jitter_tolerance_s", 1.5)),
            poll_interval_s=float(self.cfg.get("jellyfin.poll_interval_s", 0.5)),
            stall_priors=self.cfg.get("sync.stall_estimates") or None)
        w.precise_timing = bool(self.cfg.get("sync.time_lock", False))
        return w

    def bindings(self) -> list[dict]:
        return list(self._bindings)

    def poll(self, now: float) -> Activity:
        try:
            obs = self.watcher.poll(self.client)
        except JellyfinError as e:
            # one failed request must not tear a running ghost down: keep saying
            # what we last saw and let the daemon's own timers decide
            self.ok, self.error = False, str(e)
            return self.last
        self.ok, self.error = True, None
        return self.activity(obs)

    def activity(self, obs) -> Activity:
        """Pure: an Observation from the watcher becomes an Activity. Split out
        so the daemon tests can drive it on a virtual clock."""
        binding = _binding_of(obs, self._bindings)
        m = obs.model
        kind = getattr(m, "media_type", None) or "video"
        playing = bool(obs.playing and m is not None)
        if playing and kind == "music" and not self.cfg.music_output(binding):
            # the ghost would have to play the track through the speakers, so
            # it does not play at all: shown in Now Playing, but not synced
            if self._warned_no_output != m.item_id:
                self._warned_no_output = m.item_id
                log.warning("'%s' is music, but no silent output is configured for the ghost "
                            "- showing it without syncing (Display > Ghost audio output)", m.name)
            playing = False
        return Activity(
            binding=binding,
            seen=obs.seen,
            playing=playing,
            kind=kind,
            needs_ghost=True,
            plan=self._plan(binding, kind),
            ghost=self._ghost_spec(m, kind) if m is not None else None,
            obs=obs,
            event=obs.event,
            item_key=m.item_id if m is not None else "",
            title=m.name if m is not None else "")

    def _plan(self, binding: dict | None, kind: str) -> Plan:
        b = binding or {}
        # the binding's own setting first, the global one as the fallback: this
        # is what lets the Apple TV be video/subtle and a phone music/high
        want = (b.get("mode") or "").lower() or self.cfg.get("engine.huesync.mode", "video")
        # what the app must react to follows the media, not a global preference:
        # a song has no picture, an episode has no reason to be in music mode
        mode = "music" if kind == "music" else (want if want != "music" else "video")
        monitor = None if mode == "music" else (_ghost_monitor(self.cfg) or None)
        adev = None
        if mode == "music":
            adev = b.get("audio_device") or self.cfg.get("ghost.audio_device") or None
        return Plan(area_id=b.get("area_id") or None, mode=mode, monitor=monitor,
                    audio_device=adev,
                    use_audio=self.cfg.get("engine.huesync.use_audio"),
                    intensity=(b.get("intensity")
                               or self.cfg.get("engine.huesync.intensity") or None))

    def _ghost_spec(self, m, kind: str) -> GhostSpec:
        # kind is only passed for music, so every existing caller of the
        # two-argument stream_url keeps working unchanged
        dev = ""
        if kind == "music":
            url = self.client.stream_url(m.item_id, m.media_source_id, kind="music")
            b = next((x for x in self._bindings if x.get("audio_device")), None)
            dev = (b or {}).get("audio_device") or self.cfg.get("ghost.audio_device", "")
        else:
            url = self.client.stream_url(m.item_id, m.media_source_id)
        return GhostSpec(url=url, http_header=self.client.auth_header_for_mpv(), item_id=m.item_id,
                         audio_only=(kind == "music"), audio_device=dev)

    def close(self) -> None:
        pass


def _binding_of(obs, bindings: list[dict]) -> dict | None:
    """Which binding the watcher's matcher came from."""
    who = getattr(obs, "player", None)
    if who is None:
        return bindings[0] if bindings else None
    if who.binding_id:
        for b in bindings:
            if b.get("id") == who.binding_id:
                return b
    for b in bindings:
        if (b.get("device_id") or "") == (who.device_id or "") and \
                (b.get("device_name_contains") or "").lower() == (who.needle or ""):
            return b
    return bindings[0] if bindings else None


def _ghost_monitor(cfg) -> str:
    """The display the ghost plays on, as the Hue Sync app names it."""
    from ..winutil import list_displays

    name = cfg.get("ghost.screen_name", "")
    idx = cfg.get("ghost.screen_index", None)
    displays = list_displays()
    for d in displays:
        if name and d.name == name:
            return d.monitor_id
    if idx is not None and 0 <= int(idx) < len(displays):
        return displays[int(idx)].monitor_id
    return ""
