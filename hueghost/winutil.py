"""Platform helpers: display enumeration, Hue Sync app peek, autostart. Windows
does the real work; other platforms get best-effort / no-op versions.
"""
from __future__ import annotations

import json
import os
import re
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


def _bundled_mpv() -> list[str]:
    """mpv shipped next to the installed executable (installer component)."""
    base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    exe = "mpv.exe" if sys.platform == "win32" else "mpv"
    return [os.path.join(base, "mpv", exe), os.path.join(base, exe),
            os.path.join(os.path.dirname(base), "mpv", exe)]


def find_mpv(configured: str = "") -> str | None:
    if configured and configured not in ("mpv", "mpv.exe"):
        return configured if os.path.exists(configured) else None
    for c in _bundled_mpv():
        if os.path.exists(c):
            return c
    p = shutil.which("mpv")
    if p:
        return p
    if sys.platform == "win32":
        for c in MPV_CANDIDATES_WIN:
            if os.path.exists(c):
                return c
    return None


def tray_command() -> list[str]:
    """How to start Hue Ghost at login: the desktop app minimised to the tray."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        # the CLI exe (hue-ghost.exe) may have called us: start the windowed app next to it
        gui = os.path.join(os.path.dirname(exe), "HueGhost.exe")
        if os.path.exists(gui):
            exe = gui
        return [exe, "gui", "--minimized"]
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return [exe, "-m", "hueghost", "gui", "--minimized"]


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


def hue_sync_bridge_file() -> str | None:
    d = hue_sync_dir()
    return os.path.join(d, "bridge.json") if d else None


def hue_sync_selected_area() -> tuple[str | None, str | None]:
    """(area id, area name) currently selected in the Hue Sync app, from bridge.json."""
    p = hue_sync_bridge_file()
    if not p or not os.path.exists(p):
        return None, None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            br = json.loads(f.read(), strict=False)
    except Exception:
        return None, None
    sel, name = _selected_group(br)
    return sel, name


def hue_sync_groups() -> list[dict]:
    """Entertainment areas the Hue Sync app knows: [{id, name, lights}]."""
    p = hue_sync_bridge_file()
    if not p or not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            br = json.loads(f.read(), strict=False)
    except Exception:
        return []
    out = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("type") == "huestream.Group" and "Id" in o:
                out.append({"id": o["Id"], "name": o.get("Name") or o["Id"], "lights": len(o.get("Lights") or [])})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(br)
    out.sort(key=lambda g: g["name"].lower())
    return out


def hue_sync_write_selected_area(area_id: str) -> None:
    """Point the (stopped!) Hue Sync app at another entertainment area. Text
    substitution keeps the file byte-identical otherwise (it embeds a PEM with
    raw newlines that a JSON round-trip would rewrite)."""
    p = hue_sync_bridge_file()
    if not p or not os.path.exists(p):
        raise RuntimeError("Hue Sync bridge.json not found")
    with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
        raw = f.read()
    new, n = re.subn(r'("SelectedGroup"\s*:\s*")[^"]*(")', lambda m: m.group(1) + area_id + m.group(2), raw, count=1)
    if n != 1:
        raise RuntimeError("SelectedGroup not found in bridge.json")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(new)


AUDIO_MODES = ("video", "games")      # the app modes that have a "use audio" switch


def hue_sync_config_file() -> str | None:
    d = hue_sync_dir()
    return os.path.join(d, "config.json") if d else None


def hue_sync_hash_file() -> str | None:
    """``.cfg``: the FNV-1a-64 digest (as unsigned decimal) of config.json that
    the app writes beside it. A patch keeps it in step so the app does not see
    its own config as changed underneath it."""
    d = hue_sync_dir()
    return os.path.join(d, ".cfg") if d else None


def _fnv1a64(data: bytes) -> int:
    h = 0xCBF29CE484222325
    for b in data:
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def _json_object_span(raw: str, key: str, start: int = 0, end: int | None = None) -> tuple[int, int] | None:
    """Span of the ``{...}`` value of ``"key"`` inside raw[start:end], brace
    matched and string aware (so a brace inside a value cannot fool it)."""
    end = len(raw) if end is None else end
    m = re.compile(r'"%s"\s*:\s*\{' % re.escape(key)).search(raw, start, end)
    if not m:
        return None
    i = m.end() - 1
    depth, j, in_str, esc = 0, i, False, False
    while j < end:
        c = raw[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i, j + 1
        j += 1
    return None


_BOOL_AFTER_KEY = re.compile(r"\s*:\s*(true|false)")


def _with_audio_span(raw: str, mode: str) -> tuple[int, int] | None:
    """Span of the ``true``/``false`` token of ``Core.AppMode.<Mode>.WithAudio``.

    Matched only as a key of the mode object itself - the presets nested inside
    it are free to carry a key of the same name."""
    outer = _json_object_span(raw, "AppMode")
    if not outer:
        return None
    inner = _json_object_span(raw, mode.capitalize(), outer[0], outer[1])
    if not inner:
        return None
    start, end = inner
    depth, j, in_str, esc, key_at = 0, start, False, False, 0
    while j < end:
        c = raw[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
                if depth == 1 and raw[key_at:j] == "WithAudio":
                    m = _BOOL_AFTER_KEY.match(raw, j + 1, end)
                    if m:
                        return m.span(1)
        elif c == '"':
            in_str, esc, key_at = True, False, j + 1
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        j += 1
    return None


def hue_sync_with_audio(mode: str = "video") -> bool | None:
    """"Use audio for light effects" as the Hue Sync app has it for ``mode``
    (video | games), or None when it cannot be read."""
    p = hue_sync_config_file()
    if not p or (mode or "").lower() not in AUDIO_MODES or not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
            raw = f.read()
    except OSError:
        return None
    span = _with_audio_span(raw, mode.lower())
    return raw[span[0]:span[1]] == "true" if span else None


def hue_sync_write_with_audio(mode: str, enabled: bool) -> bool:
    """Set "use audio for light effects" for ``mode`` in the *stopped* app's
    config (it reads the file at start-up and would overwrite it on exit).
    Returns True when the file changed. Text substitution again: the file is
    left byte-identical apart from the one token, and the ``.cfg`` digest is
    rewritten to match."""
    mode = (mode or "").lower()
    if mode not in AUDIO_MODES:
        raise ValueError("only %s have an audio switch" % " and ".join(AUDIO_MODES))
    p = hue_sync_config_file()
    if not p or not os.path.exists(p):
        raise RuntimeError("Hue Sync config.json not found")
    with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
        raw = f.read()
    span = _with_audio_span(raw, mode)
    if not span:
        raise RuntimeError("WithAudio not found for mode %s in config.json" % mode)
    want = "true" if enabled else "false"
    if raw[span[0]:span[1]] == want:
        return False
    new = raw[:span[0]] + want + raw[span[1]:]
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    h = hue_sync_hash_file()
    if h:
        try:
            _overwrite(h, str(_fnv1a64(new.encode("utf-8"))))
        except OSError:
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(raw)        # a config the digest disowns is worse than no change
            raise
    return True


def _overwrite(path: str, text: str) -> None:
    """Replace a file's contents, keeping the file itself. Windows refuses to
    create-always over a hidden file - and ``.cfg`` is hidden - so an existing
    one is truncated in place instead."""
    with open(path, "r+" if os.path.exists(path) else "w", encoding="ascii") as f:
        f.write(text)
        f.truncate()


def hue_sync_kill() -> bool:
    if sys.platform != "win32":
        return False
    import subprocess
    r = subprocess.run(["taskkill", "/F", "/IM", "HueSync.exe"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode == 0


def hue_sync_launch(exe: str | None = None, silent: bool = True) -> bool:
    import subprocess
    exe = exe or hue_sync_exe()
    if not exe or not os.path.exists(exe):
        return False
    args = [exe] + (["-silent"] if silent else [])
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                     close_fds=True)
    return True


def hue_sync_info() -> dict:
    """What the Hue Sync app is configured to do, from its own config files."""
    info: dict = {"installed": bool(hue_sync_exe()), "exe": hue_sync_exe(), "config_dir": hue_sync_dir(),
                  "public_control_enabled": None, "public_control_port": None,
                  "automatic_display": None, "sync_delay_ms": None, "selected_area": None,
                  "selected_area_id": None, "groups": hue_sync_groups(),
                  "with_audio": {m: hue_sync_with_audio(m) for m in AUDIO_MODES}}
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
        info["selected_area_id"] = sel
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


# -- power / display state ---------------------------------------------------------
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def keep_awake(on: bool) -> bool:
    """Hold (or release) the display + system idle timers for the *calling
    thread*. Windows switches every display off after the idle timeout - the
    virtual ghost display included - and Hue Sync then captures a dead screen.
    Returns True when the request was applied (Windows only)."""
    if sys.platform != "win32":
        return False
    import ctypes
    flags = ES_CONTINUOUS | ((ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) if on else 0)
    try:
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags)))
    except Exception:
        return False


def wake_display() -> bool:
    """Turn displays that are already powered off back on: reset the display
    idle timer and inject a 1-px mouse jiggle (net zero movement), which is
    what reliably wakes a monitor the idle timeout switched off. Returns True
    when input was injected (Windows only)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(ES_DISPLAY_REQUIRED))
    except Exception:
        pass
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    INPUT_MOUSE, MOUSEEVENTF_MOVE = 0, 0x0001
    try:
        inputs = (INPUT * 2)()
        for i, dx in enumerate((1, -1)):
            inputs[i].type = INPUT_MOUSE
            inputs[i].u.mi = MOUSEINPUT(dx, 0, 0, MOUSEEVENTF_MOVE, 0, 0)
        sent = ctypes.windll.user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
        return sent == 2
    except Exception:
        return False


def desktop_locked() -> bool | None:
    """True while the workstation is locked (the secure desktop owns the input
    - nothing can capture it), False when the normal desktop is up, None when
    unknown (not Windows)."""
    if sys.platform != "win32":
        return None
    import ctypes
    DESKTOP_READOBJECTS = 0x0001
    try:
        user32 = ctypes.windll.user32
        h = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not h:
            return True
        try:
            buf = ctypes.create_unicode_buffer(64)
            n = ctypes.c_ulong(0)
            UOI_NAME = 2
            if user32.GetUserObjectInformationW(h, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(n)):
                return buf.value.lower() != "default"
            return False
        finally:
            user32.CloseDesktop(h)
    except Exception:
        return None


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
