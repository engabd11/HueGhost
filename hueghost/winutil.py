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
    monitor_id: str = ""   # MONITOR\DELA212\{4d36e96e-...}\0001 - what Hue Sync stores
    friendly: str = ""     # "Alienware AW3423DWF" / "Generic PnP Monitor"

    def label(self) -> str:
        return "%s  %dx%d at (%d,%d)%s%s" % (self.name, self.width, self.height, self.left, self.top,
                                             ("  " + self.friendly) if self.friendly else "",
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
    ids = _monitor_ids()
    for d in out:
        mid, friendly = ids.get(d.name, ("", ""))
        d.monitor_id, d.friendly = mid, friendly
    return out


def _display_device_struct():
    """Built lazily so importing this module costs nothing off Windows."""
    import ctypes
    from ctypes import wintypes

    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                    ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                    ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

    return DISPLAY_DEVICEW


def _monitor_ids() -> dict[str, tuple[str, str]]:
    r"""``{\\.\DISPLAY6: (MONITOR\MTT1337\{4d36e96e-...}\0002, "Generic PnP Monitor")}``.

    ``EnumDisplayMonitors`` (above) gives the adapter's device name; the Hue Sync
    app stores the *monitor* child device's ``DeviceID``, which only
    ``EnumDisplayDevices`` reports. Keyed by adapter name so the two line up."""
    if sys.platform != "win32":
        return {}
    import ctypes

    user32 = ctypes.windll.user32
    DISPLAY_DEVICEW = _display_device_struct()
    out: dict[str, tuple[str, str]] = {}
    i = 0
    while True:
        ad = DISPLAY_DEVICEW()
        ad.cb = ctypes.sizeof(ad)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(ad), 0):
            break
        mon = DISPLAY_DEVICEW()
        mon.cb = ctypes.sizeof(mon)
        if user32.EnumDisplayDevicesW(ad.DeviceName, 0, ctypes.byref(mon), 0):
            out[ad.DeviceName] = (mon.DeviceID, mon.DeviceString)
        i += 1
    return out


def monitor_id_at(left: int, top: int, right: int, bottom: int) -> str:
    """The monitor DeviceID a window rectangle sits on (largest overlap)."""
    best, best_area = "", 0
    for d in list_displays():
        w = min(right, d.left + d.width) - max(left, d.left)
        h = min(bottom, d.top + d.height) - max(top, d.top)
        if w > 0 and h > 0 and w * h > best_area:
            best, best_area = d.monitor_id, w * h
    return best


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

# Render endpoints are named "{0.0.0.00000000}.{guid}" by Windows (and by the Hue
# Sync app); mpv calls the same device "wasapi/{guid}".
_RENDER_PREFIX = "{0.0.0.00000000}."
_MMDEV_RENDER = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
_PKEY_NAME = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"      # friendly name
_PKEY_DESC = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"      # device description


@dataclass
class AudioOutput:
    id: str              # {0.0.0.00000000}.{guid}  - what Hue Sync stores
    guid: str            # {guid}                   - mpv: --audio-device=wasapi/{guid}
    name: str
    default: bool = False

    @property
    def mpv_device(self) -> str:
        return "wasapi/" + self.guid

    def label(self) -> str:
        return self.name + ("  [default]" if self.default else "")


# -- just enough COM to talk to the audio endpoints, in plain ctypes ----------
CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioSessionManager2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
IID_IAudioMeterInformation = "{C02216F6-8C67-4B5B-9D00-D008E73E0064}"


def _guid(s: str):
    import ctypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                    ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

    g = GUID()
    ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


def _vcall(ptr, index: int, *argtypes):
    """Bind method ``index`` of a COM object's vtable. 0/1/2 are the IUnknown
    three: QueryInterface, AddRef, Release.

    The result type is ``c_long``, not ``ctypes.HRESULT``: HRESULT makes ctypes
    *raise* on a failing call, and failing calls are normal here - a PC with no
    sound card has no default endpoint, and that must read as "no devices", not
    as an exception out of the engine's constructor."""
    import ctypes
    from ctypes import POINTER, c_void_p

    vtbl = ctypes.cast(ptr, POINTER(c_void_p))[0]
    fn = ctypes.cast(vtbl, POINTER(c_void_p))[index]
    return ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, *argtypes)(fn)


def co_initialize() -> None:
    """Per-thread, idempotent. The daemon loop thread is the only caller in the
    poll path; the web API calls it on its own threads."""
    import ctypes

    ctypes.windll.ole32.CoInitializeEx(None, 0)


def _device_enumerator():
    """A fresh IMMDeviceEnumerator, or None. Caller releases it."""
    import ctypes
    from ctypes import byref, c_void_p

    co_initialize()
    enum = c_void_p()
    hr = ctypes.windll.ole32.CoCreateInstance(byref(_guid(CLSID_MMDeviceEnumerator)), None, 1,
                                              byref(_guid(IID_IMMDeviceEnumerator)), byref(enum))
    return None if (hr or not enum) else enum


def mpv_audio_device(endpoint_id: str) -> str:
    """Windows names a render endpoint ``{0.0.0.00000000}.{guid}``; mpv calls
    the same device ``wasapi/{guid}``."""
    m = re.search(r"\{[0-9a-fA-F-]{36}\}$", endpoint_id or "")
    return "wasapi/" + m.group(0) if m else ""


def default_audio_output_id() -> str:
    """Endpoint id Windows currently plays to, or "" when there is none (a PC
    with no sound card, or the audio service stopped)."""
    if sys.platform != "win32":
        return ""
    try:
        return _default_audio_output_id()
    except Exception:
        return ""


def _default_audio_output_id() -> str:
    import ctypes
    from ctypes import POINTER, byref, c_int, c_void_p

    enum = _device_enumerator()
    if enum is None:
        return ""
    try:
        dev = c_void_p()
        # GetDefaultAudioEndpoint(eRender=0, eConsole=0)
        if _vcall(enum, 4, c_int, c_int, POINTER(c_void_p))(enum, 0, 0, byref(dev)) or not dev:
            return ""
        try:
            out = ctypes.c_wchar_p()
            if _vcall(dev, 5, POINTER(ctypes.c_wchar_p))(dev, byref(out)):   # IMMDevice::GetId
                return ""
            val = out.value or ""
            ctypes.windll.ole32.CoTaskMemFree(out)
            return val
        finally:
            _vcall(dev, 2)(dev)      # Release
    finally:
        _vcall(enum, 2)(enum)


def list_audio_outputs() -> list[AudioOutput]:
    """Active WASAPI render endpoints, read from the registry (no COM, no
    PROPVARIANT marshalling). This is the list the ghost can play music into and
    the list the Hue Sync app can listen to in music mode. Empty on a PC with
    no sound card - which is a normal state, not an error."""
    if sys.platform != "win32":
        return []
    import winreg

    default = default_audio_output_id()
    out: list[AudioOutput] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _MMDEV_RENDER)
    except OSError:
        return []
    with root:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, sub) as k:
                    state, _ = winreg.QueryValueEx(k, "DeviceState")
                    if state != 1:          # 1 = DEVICE_STATE_ACTIVE
                        continue
                    name = ""
                    with winreg.OpenKey(k, "Properties") as pk:
                        for key in (_PKEY_NAME, _PKEY_DESC):
                            try:
                                v, _ = winreg.QueryValueEx(pk, key)
                                name = (name + " (" + v + ")") if name else v
                            except OSError:
                                pass
            except OSError:
                continue
            eid = _RENDER_PREFIX + sub
            out.append(AudioOutput(id=eid, guid=sub, name=name or sub, default=(eid == default)))
    out.sort(key=lambda a: (not a.default, a.name.lower()))
    return out


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
_STR_AFTER_KEY = re.compile(r'\s*:\s*"((?:[^"\\]|\\.)*)"')
_STR_IN_ARRAY_AFTER_KEY = re.compile(r'\s*:\s*\[\s*"((?:[^"\\]|\\.)*)"')


def _member_span(raw: str, key: str, start: int, end: int, pat) -> tuple[int, int] | None:
    """Span of the value token of ``"key"`` when it is a *direct* member of the
    object ``raw[start:end]`` - nested objects are free to carry the same key.
    ``pat`` matches just after the key's closing quote; group 1 is the token."""
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
                if depth == 1 and raw[key_at:j] == key:
                    m = pat.match(raw, j + 1, end)
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


def _section_span(raw: str, *path: str) -> tuple[int, int] | None:
    """Span of a nested object, e.g. ``("Core", "AppMode", "Video")``."""
    span = (0, len(raw))
    for key in path:
        span = _json_object_span(raw, key, span[0], span[1])
        if not span:
            return None
    return span


_backed_up = False


def _patch_app_config(edit) -> bool:
    """Apply ``edit(raw) -> new raw | None`` to the *stopped* app's config.json
    and keep the ``.cfg`` digest in step. The file is left byte-identical apart
    from the tokens ``edit`` replaced. Returns True when it changed."""
    p = hue_sync_config_file()
    if not p or not os.path.exists(p):
        raise RuntimeError("Hue Sync config.json not found")
    with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
        raw = f.read()
    new = edit(raw)
    if not new or new == raw:
        return False
    global _backed_up
    if not _backed_up:              # once per process: the app's own settings live here
        _backed_up = True
        try:
            with open(p + ".hueghost.bak", "w", encoding="utf-8", newline="") as f:
                f.write(raw)
        except OSError:
            pass                    # a backup we cannot write must not stop the patch
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


def _replace_spans(raw: str, edits: list[tuple[tuple[int, int], str]]) -> str:
    """Apply (span, replacement) edits right-to-left so earlier spans stay valid."""
    for (a, b), text in sorted(edits, key=lambda e: e[0][0], reverse=True):
        raw = raw[:a] + text + raw[b:]
    return raw


def _json_escape(s: str) -> str:
    return json.dumps(s)[1:-1]


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
    return _member_span(raw, "WithAudio", inner[0], inner[1], _BOOL_AFTER_KEY)


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
    Returns True when the file changed."""
    return hue_sync_patch(with_audio=((mode or "").lower(), enabled))


# -- the two settings the app only reads at start-up: capture display, audio in --
def _preferred_monitor_span(raw: str) -> tuple[int, int] | None:
    """Span of the string *inside* ``App.PreferredMonitor: [ "..." ]``."""
    app = _json_object_span(raw, "App")
    if not app:
        return None
    return _member_span(raw, "PreferredMonitor", app[0], app[1], _STR_IN_ARRAY_AFTER_KEY)


def _core_str_span(raw: str, key: str) -> tuple[int, int] | None:
    core = _json_object_span(raw, "Core")
    return _member_span(raw, key, core[0], core[1], _STR_AFTER_KEY) if core else None


def _core_bool_span(raw: str, key: str) -> tuple[int, int] | None:
    core = _json_object_span(raw, "Core")
    return _member_span(raw, key, core[0], core[1], _BOOL_AFTER_KEY) if core else None


def hue_sync_preferred_monitor() -> str | None:
    """Monitor DeviceID the Hue Sync app captures, or None when unreadable."""
    p = hue_sync_config_file()
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
            raw = f.read()
    except OSError:
        return None
    span = _preferred_monitor_span(raw)
    return json.loads('"%s"' % raw[span[0]:span[1]]) if span else None


def hue_sync_audio_device() -> str | None:
    """Render endpoint the Hue Sync app listens to in music mode."""
    p = hue_sync_config_file()
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
            raw = f.read()
    except OSError:
        return None
    span = _core_str_span(raw, "PreferredAudioDevice")
    return json.loads('"%s"' % raw[span[0]:span[1]]) if span else None


AUTO = "auto"       # "let the app choose again", as opposed to pinning a value


def hue_sync_patch(*, with_audio: tuple[str, bool] | None = None, monitor: str | None = None,
                   audio_device: str | None = None) -> bool:
    """Apply every start-up-only setting the app has in **one** read, one write
    and one digest rewrite - a sync session restarts the app at most once, so it
    must not leave intermediate states on disk in between.

    ``monitor`` / ``audio_device`` take a value to pin, or ``AUTO`` to hand the
    choice back to the app (the flag it reads at start-up, not the value)."""

    def edit(raw: str) -> str | None:
        edits: list[tuple[tuple[int, int], str]] = []

        if with_audio is not None:
            mode, on = with_audio
            if mode.lower() not in AUDIO_MODES:
                raise ValueError("only %s have an audio switch" % " and ".join(AUDIO_MODES))
            span = _with_audio_span(raw, mode.lower())
            if not span:
                raise RuntimeError("WithAudio not found for mode %s in config.json" % mode)
            want = "true" if on else "false"
            if raw[span[0]:span[1]] != want:
                edits.append((span, want))

        for value, span_of, auto_key, what in (
                (monitor, _preferred_monitor_span, "AutomaticDisplay", "App.PreferredMonitor"),
                (audio_device, lambda r: _core_str_span(r, "PreferredAudioDevice"),
                 "AutomaticAudioDevice", "Core.PreferredAudioDevice")):
            if value is None:
                continue
            auto = _core_bool_span(raw, auto_key)
            if value == AUTO:
                if auto and raw[auto[0]:auto[1]] != "true":
                    edits.append((auto, "true"))
                continue
            span = span_of(raw)
            if not span:
                raise RuntimeError("%s not found in config.json" % what)
            want = _json_escape(value)
            if raw[span[0]:span[1]] != want:
                edits.append((span, want))
            # a pinned value is only honoured while the app is not choosing its own
            if auto and raw[auto[0]:auto[1]] != "false":
                edits.append((auto, "false"))

        return _replace_spans(raw, edits) if edits else None

    return _patch_app_config(edit)


def hue_sync_write_preferred_monitor(monitor_id: str) -> bool:
    """Point the *stopped* app's capture at another display."""
    if not monitor_id:
        raise ValueError("monitor_id is empty")
    return hue_sync_patch(monitor=monitor_id)


def hue_sync_write_audio_device(endpoint_id: str) -> bool:
    """Point the *stopped* app's music mode at a render endpoint (what the ghost
    plays remote music into)."""
    if not endpoint_id:
        raise ValueError("endpoint_id is empty")
    return hue_sync_patch(audio_device=endpoint_id)


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
