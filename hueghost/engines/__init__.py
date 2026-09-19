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
    bri: int | None = None
    error: str | None = None
    area_id: str | None = None     # entertainment area the engine currently targets
    area_name: str | None = None
    switching: bool = False        # area switch in progress
    updated_mono: float = field(default_factory=time.monotonic)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("updated_mono", None)
        return d


class Engine:
    name = "none"

    def start(self) -> None:
        """Ask for sync ON (idempotent; engines reconcile in the background)."""

    def stop(self) -> None:
        """Ask for sync OFF."""

    def state(self) -> EngineState:
        return EngineState(name=self.name, connected=True, syncing=False)

    def set_intensity(self, level: str) -> None:
        pass

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


def build_engine(cfg) -> Engine:
    kind = str(cfg.get("engine.type", "huesync") or "none").lower()
    if kind == "huesync":
        from .huesync import HueSyncEngine
        h = cfg.section("engine").get("huesync", {})
        return HueSyncEngine(
            host=h.get("host", "127.0.0.1"), port=int(h.get("port", 24851)),
            mode=h.get("mode", "video"), intensity=h.get("intensity", ""),
            launch_exe=h.get("launch_exe", ""))
    if kind == "httphook":
        url = cfg.get("engine.httphook.url", "")
        if url:
            return HttpHookEngine(url)
        log.warning("engine.type=httphook but engine.httphook.url is empty -> no engine")
    return NullEngine()
