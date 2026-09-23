"""Light engines: whatever turns the ghost display into light.

``Engine`` is deliberately small so a future self-contained engine (own colour
extraction + DTLS entertainment stream, "Route B") can slot in next to the
Hue Sync app driver without touching the daemon.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Callable

log = logging.getLogger("hue-ghost.engine")


@dataclass
class EngineState:
    name: str = "none"
    connected: bool = False
    syncing: bool = False
    desired_sync: bool = False
    state: str | None = None       # engine-specific raw state
    mode: str | None = None
    intensity: str | None = None
    use_audio: bool | None = None  # the app's "use audio for light effects" for `mode`
    # the same switch for *every* mode that has one: a session pre-applies all of
    # them, which is what makes a mid-session mode change free
    audio_modes: dict[str, bool | None] = field(default_factory=dict)
    bri: int | None = None
    error: str | None = None
    area_id: str | None = None     # entertainment area the engine currently targets
    area_name: str | None = None
    monitor_id: str | None = None  # display the engine's app captures
    monitor_name: str | None = None
    audio_device_id: str | None = None    # render endpoint it listens to in music mode
    audio_device_name: str | None = None
    # "the app is choosing this itself" - what AUTO is asking for, and the only
    # way to tell whether AUTO has already been applied
    monitor_auto: bool | None = None
    audio_device_auto: bool | None = None
    switching: bool = False        # area switch in progress
    updated_mono: float = field(default_factory=time.monotonic)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("updated_mono", None)
        return d


AUTO = "auto"      # "let the app choose again", as opposed to pinning a value


@dataclass(frozen=True)
class Plan:
    """What the engine must be set to for one activity. ``None`` anywhere means
    "leave whatever the app has"; ``AUTO`` means "hand the choice back to it"."""
    area_id: str | None = None
    mode: str | None = None
    monitor: str | None = None          # AUTO | a monitor DeviceID
    audio_device: str | None = None     # AUTO | a render endpoint id
    use_audio: bool | None = None
    intensity: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class Engine:
    name = "none"

    def apply_plan(self, plan: Plan) -> None:
        """Set everything one activity wants, at once. Engines that restart an
        app to apply start-up-only settings must do this under a single lock:
        applied piecemeal, a new area and a new display are two restarts."""
        if plan.mode:
            self.set_mode(plan.mode)
        if plan.intensity:
            self.set_intensity(plan.intensity)
        self.set_use_audio(plan.use_audio)
        self.set_area(plan.area_id)

    def set_brightness(self, level: int) -> None:
        """Absolute 0-100. The Hue Sync protocol only has a signed step, so this
        is synthesised from the level the app reports."""

    def start(self) -> None:
        """Ask for sync ON (idempotent; engines reconcile in the background)."""

    def stop(self) -> None:
        """Ask for sync OFF."""

    def state(self) -> EngineState:
        return EngineState(name=self.name, connected=True, syncing=False)

    def set_intensity(self, level: str) -> None:
        pass

    def set_mode(self, mode: str) -> None:
        """Video / music / games: what the engine reacts to."""

    def set_use_audio(self, on: bool | None) -> None:
        """Let the lights react to audio as well as the picture (None = leave
        whatever the engine's own app is set to)."""

    def adjust_brightness(self, step: int) -> None:
        pass

    def set_area(self, area_id: str | None) -> None:
        """Ask the engine to target this entertainment area (None = leave as is)."""

    def areas(self) -> list[dict]:
        return []

    def wait_until(self, pred: Callable[[EngineState], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred(self.state()):
                return True
            time.sleep(0.1)
        return pred(self.state())

    def alive(self) -> bool:
        """Whether the engine's background worker is running. The daemon
        watchdog rebuilds the engine when this turns False."""
        return True

    def close(self) -> None:
        pass


class NullEngine(Engine):
    """No engine: the ghost plays; the user drives Hue Sync (or anything) by hand."""
    name = "none"

    def __init__(self):
        self._want = False

    def start(self) -> None:
        self._want = True

    def stop(self) -> None:
        self._want = False

    def state(self) -> EngineState:
        return EngineState(name=self.name, connected=True, syncing=self._want, desired_sync=self._want)


class HttpHookEngine(Engine):
    """GET <url>/start and <url>/stop (Huestacean-style local HTTP API, or any webhook)."""
    name = "httphook"

    def __init__(self, base_url: str, timeout: float = 3.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._st = EngineState(name=self.name)
        self._lock = threading.Lock()

    def _call(self, action: str) -> None:
        try:
            urllib.request.urlopen(self.base + "/" + action, timeout=self.timeout)
            with self._lock:
                self._st.connected = True
                self._st.error = None
                self._st.syncing = action == "start"
                self._st.state = "syncing" if action == "start" else "idle"
            log.info("httphook %s ok", action)
        except Exception as e:
            with self._lock:
                self._st.connected = False
                self._st.error = str(e)
            log.warning("httphook %s failed: %s", action, e)

    def start(self) -> None:
        with self._lock:
            self._st.desired_sync = True
        threading.Thread(target=self._call, args=("start",), daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            self._st.desired_sync = False
        self._call("stop")

    def state(self) -> EngineState:
        with self._lock:
            return EngineState(**{**self._st.__dict__})


def build_engine(cfg, on_app_change: Callable[[dict], None] | None = None) -> Engine:
    """``on_app_change`` is called with e.g. ``{"mode": "music"}`` when the user
    changes something in the engine's own app and hue-ghost adopts it."""
    kind = str(cfg.get("engine.type", "huesync") or "none").lower()
    if kind == "huesync":
        from .huesync import HueSyncEngine
        h = cfg.section("engine").get("huesync", {})
        use_audio = h.get("use_audio", None)
        return HueSyncEngine(
            host=h.get("host", "127.0.0.1"), port=int(h.get("port", 24851)),
            mode=h.get("mode", "video"), intensity=h.get("intensity", ""),
            use_audio=None if use_audio is None else bool(use_audio),
            manage_area=bool(h.get("manage_area", True)),
            manage_monitor=bool(h.get("manage_monitor", True)),
            manage_audio_device=bool(h.get("manage_audio_device", True)),
            launch_exe=h.get("launch_exe", ""),
            on_app_change=on_app_change)
    if kind == "httphook":
        url = cfg.get("engine.httphook.url", "")
        if url:
            return HttpHookEngine(url)
        log.warning("engine.type=httphook but engine.httphook.url is empty -> no engine")
    return NullEngine()
