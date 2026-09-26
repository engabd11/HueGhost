"""System-tray front end (optional: pip install "hue-ghost[tray]").

Icon colour = state: grey idle, blue ghosting, green syncing, red engine error,
dark grey disabled. Menu: enable/disable, intensity, offset +/- 0.25 s, open
config / log, quit.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import webbrowser

import pystray
from PIL import Image, ImageDraw

from .config import INTENSITIES, Config, log_path
from .daemon import GHOSTING, IDLE, SYNCING, Daemon

log = logging.getLogger("hue-ghost.tray")

COLORS = {
    "disabled": (90, 90, 90),
    IDLE: (150, 150, 150),
    GHOSTING: (60, 130, 230),
    SYNCING: (60, 190, 90),
    "error": (220, 70, 60),
}


def make_icon(rgb: tuple[int, int, int], size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size // 8
    d.ellipse((pad, pad, size - pad, size - pad), fill=rgb + (255,), outline=(255, 255, 255, 200), width=3)
    # little ghost "eyes"
    e = size // 10
    d.ellipse((size * 0.35 - e, size * 0.45 - e, size * 0.35 + e, size * 0.45 + e), fill=(255, 255, 255, 230))
    d.ellipse((size * 0.65 - e, size * 0.45 - e, size * 0.65 + e, size * 0.45 + e), fill=(255, 255, 255, 230))
    return img


def _open(path: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            webbrowser.open("file://" + path)
    except Exception as e:
        log.warning("could not open %s: %s", path, e)


def run_tray(cfg: Config) -> int:
    probs = cfg.problems()
    if probs:
        for p in probs:
            log.error("config: %s", p)
        log.error("run: hue-ghost setup")
        return 2
    daemon = Daemon(cfg)
    t = threading.Thread(target=daemon.run, name="hue-ghost-daemon", daemon=True)
    t.start()

    def state_key() -> str:
        st = daemon.status()
        if not st["enabled"]:
            return "disabled"
        eng = st["engine"]
        if st["state"] != IDLE and eng.get("error") and not eng.get("connected"):
            return "error"
        return st["state"]

    def tooltip() -> str:
        st = daemon.status()
        f = st["follow"]
        if not st["enabled"]:
            return "hue-ghost: disabled"
        if st["state"] == IDLE:
            return "hue-ghost: idle (%s)" % ("client idle" if f["seen"] else "client not connected")
        d = st.get("drift_s")
        return "hue-ghost: %s '%s'  drift %s s%s  offset %+.2fs" % (
            st["state"], (f["item"] or "?")[:40], ("%+.2f" % d) if d is not None else "?",
            " (locked)" if (st.get("time_lock") or {}).get("engaged") else "", st["offset_s"])

    icon = pystray.Icon("hue-ghost", make_icon(COLORS[IDLE]), "hue-ghost")

    def set_enabled(on: bool):
        daemon.action("on" if on else "off", {})

    def set_intensity(level: str):
        daemon.action("set", {"intensity": level})

    def nudge(delta: float):
        daemon.action("set", {"offset_delta": delta})

    def quit_():
        daemon.stop()
        t.join(timeout=5)
        icon.stop()

    def intensity_item(level: str):
        return pystray.MenuItem(level.capitalize(), lambda: set_intensity(level),
                                checked=lambda item: daemon.cfg.get("engine.huesync.intensity") == level,
                                radio=True)

    icon.menu = pystray.Menu(
        pystray.MenuItem(lambda item: tooltip(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Sync enabled", lambda: set_enabled(not daemon.enabled),
                         checked=lambda item: daemon.enabled),
        pystray.MenuItem("Intensity", pystray.Menu(*[intensity_item(l) for l in INTENSITIES])),
        pystray.MenuItem(lambda item: "Offset %+.2f s" % daemon.offset, pystray.Menu(
            pystray.MenuItem("+0.25 s (lights later)", lambda: nudge(0.25)),
            pystray.MenuItem("-0.25 s (lights earlier)", lambda: nudge(-0.25)),
            pystray.MenuItem("+0.05 s", lambda: nudge(0.05)),
            pystray.MenuItem("-0.05 s", lambda: nudge(-0.05)),
        )),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open config", lambda: _open(daemon.cfg.path or "")),
        pystray.MenuItem("Open log", lambda: _open(log_path())),
        pystray.MenuItem("Reload config", lambda: daemon.action("reload", {})),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_),
    )

    def refresher():
        last = None
        while t.is_alive():
            try:
                key = state_key()
                if key != last:
                    icon.icon = make_icon(COLORS.get(key, COLORS[IDLE]))
                    last = key
                icon.title = tooltip()[:127]
            except Exception:
                pass
            threading.Event().wait(2.0)
        icon.stop()

    threading.Thread(target=refresher, name="tray-refresh", daemon=True).start()
    icon.run()
    daemon.stop()
    return 0
