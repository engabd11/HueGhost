"""Platform helpers: display enumeration, Hue Sync app peek, autostart. Windows
does the real work; other platforms get best-effort / no-op versions.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass


@dataclass
class Display:
    name: str            # \\.\DISPLAY3  (what mpv --fs-screen-name wants)
    index: int           # enumeration order (mpv --fs-screen=N is the same order)
    width: int
    height: int
    left: int
    top: int
    primary: bool

    def label(self) -> str:
        return "%s  %dx%d at (%d,%d)%s" % (self.name, self.width, self.height, self.left, self.top,
                                            "  [primary]" if self.primary else "")


def list_displays() -> list[Display]:
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    MONITORINFOF_PRIMARY = 1

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    out: list[Display] = []
    MonitorEnumProc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                         ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def cb(hmon, hdc, lprc, lparam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            out.append(Display(mi.szDevice, len(out), r.right - r.left, r.bottom - r.top,
                               r.left, r.top, bool(mi.dwFlags & MONITORINFOF_PRIMARY)))
        return 1

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(cb), 0)
    return out


# -- mpv -------------------------------------------------------------------------
MPV_CANDIDATES_WIN = [
    r"C:\Program Files\MPV Player\mpv.exe",
    r"C:\Program Files\mpv\mpv.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\mpv\mpv.exe"),
    os.path.expandvars(r"%USERPROFILE%\scoop\apps\mpv\current\mpv.exe"),
    os.path.expandvars(r"%USERPROFILE%\scoop\shims\mpv.exe"),
    r"C:\ProgramData\chocolatey\bin\mpv.exe",
]


def find_mpv(configured: str = "") -> str | None:
    if configured and configured not in ("mpv", "mpv.exe"):
        return configured if os.path.exists(configured) else None
    p = shutil.which("mpv")
    if p:
        return p
    if sys.platform == "win32":
        for c in MPV_CANDIDATES_WIN:
            if os.path.exists(c):
                return c
    return None


# -- Hue Sync app peek (read-only) ------------------------------------------------
def hue_sync_dir() -> str | None:
    if sys.platform == "win32":
        d = os.path.join(os.environ.get("APPDATA", ""), "HueSync")
    elif sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Application Support/HueSync")
    else:
        return None
    return d if os.path.isdir(d) else None


def hue_sync_exe() -> str | None:
    if sys.platform == "win32":
        for c in (r"C:\Program Files\Hue Sync\HueSync.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Programs\Hue Sync\HueSync.exe")):
            if os.path.exists(c):
                return c
    elif sys.platform == "darwin":
        c = "/Applications/Hue Sync.app"
        if os.path.exists(c):
            return c
    return None


def hue_sync_info() -> dict:
    """What the Hue Sync app is configured to do, from its own config files."""
    info: dict = {"installed": bool(hue_sync_exe()), "exe": hue_sync_exe(), "config_dir": hue_sync_dir(),
                  "public_control_enabled": None, "public_control_port": None,
                  "automatic_display": None, "sync_delay_ms": None, "selected_area": None}
    d = hue_sync_dir()
    if not d:
        return info
    try:
        with open(os.path.join(d, "config.json"), "r", encoding="utf-8", errors="replace") as f:
            cfg = json.loads(f.read(), strict=False)
        flat = _flatten(cfg)
        info["public_control_enabled"] = flat.get("PublicControlEnabled")
        info["public_control_port"] = flat.get("PublicControlPort")
        info["automatic_display"] = flat.get("AutomaticDisplay")
        info["sync_delay_ms"] = flat.get("SyncDelay")
    except Exception:
        pass
    try:
        with open(os.path.join(d, "bridge.json"), "r", encoding="utf-8", errors="replace") as f:
            br = json.loads(f.read(), strict=False)   # embeds a PEM with raw newlines
        sel, name = _selected_group(br)
        info["selected_area"] = name or sel
    except Exception:
        pass
    return info


def _flatten(obj, out: dict | None = None) -> dict:
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _flatten(v, out)
            else:
                out.setdefault(k, v)
    elif isinstance(obj, list):
        for v in obj:
            _flatten(v, out)
    return out


def _selected_group(br) -> tuple[str | None, str | None]:
    sel = None
    groups: list[dict] = []

    def walk(o):
        nonlocal sel
        if isinstance(o, dict):
            if "SelectedGroup" in o and isinstance(o["SelectedGroup"], str) and sel is None:
                sel = o["SelectedGroup"]
            if o.get("type") == "huestream.Group" and "Id" in o:
                groups.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(br)
    if sel:
        for g in groups:
            if g.get("Id") == sel:
                return sel, g.get("Name")
    return sel, None


# -- autostart -------------------------------------------------------------------
AUTOSTART_NAME = "HueGhost"


def _startup_dir() -> str:
    return os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")


def autostart_path() -> str:
    return os.path.join(_startup_dir(), AUTOSTART_NAME + ".vbs")


def install_autostart(command: list[str]) -> str:
    """Hidden-window launcher in the user's Startup folder (no admin needed)."""
    if sys.platform != "win32":
        raise RuntimeError("autostart install is Windows-only for now; use a login item / systemd --user")
    quoted = " ".join('""%s""' % c if " " in c else c for c in command)
    vbs = ('Set sh = CreateObject("WScript.Shell")\n'
           'sh.Run "%s", 0, False\n' % quoted)
    path = autostart_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(vbs)
    return path


def uninstall_autostart() -> bool:
    p = autostart_path()
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def autostart_installed() -> bool:
    return os.path.exists(autostart_path())
