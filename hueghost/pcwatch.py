"""Is an app on this PC playing something?

Two signals, both cheap and both scoped to the executables the user actually
bound - there is no scan of every process, and no guessing about what is a
"game" or a "video app":

  sound      a WASAPI session belonging to that exe is above the noise floor
  fullscreen that exe owns the foreground window and it covers a whole monitor

The exe ``*`` means *any app*: any sound, any window filling a display.
``huesync.exe`` means the same for the window signal - Hue Sync's own window is
never a fullscreen video, and binding it is how people say "this PC" (its
loopback capture already makes its sound session mirror the whole PC).

``PcProbe.observe`` is pure, like ``SessionWatcher.observe``: it takes the two
readings and the clock and returns what is playing. The OS calls live in
``audio_sessions`` / ``foreground_window`` so the tests never touch COM.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field

log = logging.getLogger("hue-ghost.pc")

ANY_APP = "*"
# windows that fill a display without anything playing: the desktop, the
# taskbar, the lock screen, start/search - never "a fullscreen video"
SHELL_CLASSES = {"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd"}
SHELL_EXES = {"explorer.exe", "lockapp.exe", "searchhost.exe", "searchapp.exe",
              "startmenuexperiencehost.exe", "shellexperiencehost.exe", "textinputhost.exe",
              "hueghost.exe"}


def any_app(exe: str, signal: str) -> bool:
    """Whether a binding's exe stands for every app, for this signal."""
    return exe == ANY_APP or (signal == "window" and exe == "huesync.exe")


@dataclass(frozen=True)
class AudioHit:
    pid: int
    exe: str                 # basename, lower case
    peak: float


@dataclass(frozen=True)
class Foreground:
    pid: int
    exe: str                 # basename, lower case
    title: str = ""
    fullscreen: bool = False
    monitor_id: str = ""     # the display it is on, as the Hue Sync app names it
    cls: str = ""            # window class, lower case

    @property
    def shell(self) -> bool:
        """The desktop, the taskbar, the lock screen, or Hue Ghost itself."""
        return self.cls in SHELL_CLASSES or self.exe in SHELL_EXES or self.pid == os.getpid()


@dataclass
class Hit:
    """What one binding looks like right now. ``since``/``last_seen`` are None
    for "never" - 0.0 would be a real instant on a clock that starts at zero."""
    playing: bool = False
    since: float | None = None       # when the signal first appeared (start_confirm_s)
    last_seen: float | None = None   # when it was last true (audio_hold_s)
    peak: float = 0.0
    title: str = ""
    monitor_id: str = ""
    running: bool = False


class PcProbe:
    def __init__(self, peak: float = 0.002, hold_s: float = 3.0, confirm_s: float = 1.0):
        self.peak = float(peak)
        self.hold_s = float(hold_s)
        self.confirm_s = float(confirm_s)
        self.hits: dict[str, Hit] = {}
        self.ignore_pids: set[int] = set()

    def observe(self, bindings: list[dict], sessions: list[AudioHit],
                fg: Foreground | None, now: float) -> dict[str, Hit]:
        """Pure. ``bindings`` are PC bindings; returns {binding id: Hit}."""
        out: dict[str, Hit] = {}
        for b in bindings:
            exe = (b.get("exe") or "").lower()
            detect = b.get("detect") or "audio"
            hit = self.hits.get(b["id"]) or Hit()
            any_sound, any_window = any_app(exe, "audio"), any_app(exe, "window")
            loud = max((s.peak for s in sessions
                        if (any_sound or s.exe == exe) and s.pid not in self.ignore_pids
                        and s.pid != os.getpid()), default=0.0)
            by_audio = loud > self.peak
            fg_mine = bool(fg and fg.pid not in self.ignore_pids
                           and (fg.exe == exe or (any_window and not fg.shell)))
            by_window = bool(fg_mine and fg.fullscreen)
            signal = (by_audio if detect == "audio" else
                      by_window if detect == "fullscreen" else
                      (by_audio or by_window))
            hit.running = bool(loud > 0.0 or fg_mine) or signal
            hit.peak = loud
            if fg_mine:
                hit.title = fg.title or hit.title
                hit.monitor_id = fg.monitor_id or hit.monitor_id
            if signal:
                if hit.last_seen is None:
                    hit.since = now
                hit.last_seen = now
            # sound comes and goes inside a film; a window does not, but an
            # alt-tab flash should not restart the Hue Sync app either
            held = hit.last_seen is not None and (now - hit.last_seen) <= self.hold_s
            confirmed = hit.since is not None and (now - hit.since) >= self.confirm_s
            hit.playing = bool(held and confirmed)
            if not held:
                hit.since = hit.last_seen = None
                hit.playing = False
            self.hits[b["id"]] = hit
            out[b["id"]] = hit
        return out

    def poll(self, bindings: list[dict], now: float) -> dict[str, Hit]:
        """The same, reading the OS. Only the signals some binding asks for are
        read, so an install with no PC bindings costs nothing at all."""
        wanted = {(b.get("exe") or "").lower() for b in bindings}
        if any(any_app(e, "audio") for e in wanted):
            wanted = None                   # every app's sound counts
        need_audio = any((b.get("detect") or "audio") in ("audio", "either") for b in bindings)
        need_fg = any((b.get("detect") or "audio") in ("fullscreen", "either") for b in bindings)
        sessions = audio_sessions(wanted) if need_audio else []
        fg = foreground_window() if need_fg else None
        return self.observe(bindings, sessions, fg, now)


# -- the OS side ------------------------------------------------------------
_exe_cache: dict[int, tuple[str, float]] = {}
_EXE_TTL_S = 60.0          # pids are recycled; do not trust one forever


def exe_of(pid: int) -> str:
    """Basename of a pid's executable, cached. Steady state costs no syscall."""
    if pid <= 0:
        return ""
    now = time.monotonic()
    hit = _exe_cache.get(pid)
    if hit and now - hit[1] < _EXE_TTL_S:
        return hit[0]
    name = _query_exe(pid)
    _exe_cache[pid] = (name, now)
    if len(_exe_cache) > 512:
        for k in [k for k, v in _exe_cache.items() if now - v[1] > _EXE_TTL_S]:
            _exe_cache.pop(k, None)
    return name


def _query_exe(pid: int) -> str:
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""                       # protected process: not ours to look at
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = wintypes.DWORD(32768)
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return ""
        return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        k32.CloseHandle(h)


def exe_path_of(pid: int) -> str:
    """Full path of a pid's executable - what the version resource needs."""
    if sys.platform != "win32" or pid <= 0:
        return ""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))    # QUERY_LIMITED_INFORMATION
    if not h:
        return ""                       # protected process: not ours to look at
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = wintypes.DWORD(32768)
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return ""
        return buf.value
    finally:
        k32.CloseHandle(h)


_desc_cache: dict[str, str] = {}


def _file_description(path: str) -> str:
    """The "FileDescription" a Windows binary carries - "Google Chrome" for
    chrome.exe. Empty when the file has no version resource at all, which is
    common for games and for anything built without one."""
    if sys.platform != "win32" or not path:
        return ""
    if path in _desc_cache:
        return _desc_cache[path]
    out = ""
    try:
        import ctypes
        from ctypes import wintypes
        ver = ctypes.windll.version
        ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        ver.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
        size = ver.GetFileVersionInfoSizeW(path, None)
        if size:
            buf = ctypes.create_string_buffer(size)
            if ver.GetFileVersionInfoW(path, 0, size, buf):
                ptr, ln = ctypes.c_void_p(), wintypes.UINT()
                # a binary declares which language its strings are in; asking for
                # the wrong one gives nothing, so read the table rather than guess
                if ver.VerQueryValueW(buf, r"\VarFileInfo\Translation",
                                      ctypes.byref(ptr), ctypes.byref(ln)) and ln.value >= 4:
                    lang, cp = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD * 2)).contents
                    key = r"\StringFileInfo\%04x%04x\FileDescription" % (lang, cp)
                    if ver.VerQueryValueW(buf, key, ctypes.byref(ptr), ctypes.byref(ln)) and ln.value:
                        out = ctypes.wstring_at(ptr, ln.value).split(chr(0))[0].strip()
    except Exception:
        out = ""                        # a name is a nicety; never fail a listing for it
    _desc_cache[path] = out
    return out


def app_name(exe: str, path: str = "") -> str:
    """What a person calls the app, not what the process is called.

    "chrome.exe" is a poor thing to pick from a list when three browsers are
    running; "Google Chrome" is not. Falls back to a tidied-up file name for
    binaries with no version resource."""
    desc = _file_description(path)
    if desc and desc.lower() not in (exe.lower(), (exe or "")[:-4].lower()):
        return desc
    stem = exe[:-4] if exe.lower().endswith(".exe") else exe
    stem = stem.replace("_", " ").replace("-", " ").strip()
    return stem[:1].upper() + stem[1:] if stem else exe


def foreground_window() -> Foreground | None:
    """The focused window, its process, and whether it covers a whole display."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    u32 = ctypes.windll.user32

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    title = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(hwnd, title, 512)
    cls = ctypes.create_unicode_buffer(128)
    u32.GetClassNameW(hwnd, cls, 128)
    r = wintypes.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    mi = MONITORINFOEXW()
    mi.cbSize = ctypes.sizeof(mi)
    hmon = u32.MonitorFromWindow(hwnd, 2)          # MONITOR_DEFAULTTONEAREST
    full, monitor_id = False, ""
    if hmon and u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        m = mi.rcMonitor
        full = (r.left <= m.left and r.top <= m.top
                and r.right >= m.right and r.bottom >= m.bottom)
        from .winutil import list_displays
        monitor_id = next((d.monitor_id for d in list_displays() if d.name == mi.szDevice), "")
    return Foreground(pid=pid.value, exe=exe_of(pid.value), title=title.value,
                      fullscreen=full, monitor_id=monitor_id, cls=cls.value.lower())


def audio_sessions(wanted: set[str] | None = None) -> list[AudioHit]:
    """Processes currently playing sound on the default output device.

    Each session hands us its pid directly, so no process enumeration is
    needed; ``wanted`` drops the rest before the pid is even resolved."""
    if sys.platform != "win32":
        return []
    try:
        return _audio_sessions(wanted)
    except Exception as e:                  # a COM failure must not stop the daemon
        log.debug("audio session enumeration failed: %s", e, exc_info=True)
        return []


def _audio_sessions(wanted: set[str] | None) -> list[AudioHit]:
    import ctypes
    from ctypes import POINTER, byref, c_float, c_int, c_ulong, c_void_p

    from .winutil import (IID_IAudioMeterInformation, IID_IAudioSessionManager2, _device_enumerator,
                          _guid, _vcall)

    CLSCTX_ALL = 23
    out: list[AudioHit] = []
    enum = _device_enumerator()
    if enum is None:
        return out
    try:
        dev = c_void_p()
        if _vcall(enum, 4, c_int, c_int, POINTER(c_void_p))(enum, 0, 0, byref(dev)) or not dev:
            return out
        try:
            mgr = c_void_p()
            iid = _guid(IID_IAudioSessionManager2)
            if _vcall(dev, 3, c_void_p, c_ulong, c_void_p, POINTER(c_void_p))(
                    dev, ctypes.cast(byref(iid), c_void_p), CLSCTX_ALL, None, byref(mgr)) or not mgr:
                return out
            try:
                sess = c_void_p()
                if _vcall(mgr, 5, POINTER(c_void_p))(mgr, byref(sess)) or not sess:
                    return out
                try:
                    n = c_int()
                    _vcall(sess, 3, POINTER(c_int))(sess, byref(n))
                    for i in range(n.value):
                        ctl = c_void_p()
                        if _vcall(sess, 4, c_int, POINTER(c_void_p))(sess, i, byref(ctl)) or not ctl:
                            continue
                        try:
                            pid = c_ulong()
                            if _vcall(ctl, 14, POINTER(c_ulong))(ctl, byref(pid)):
                                continue
                            exe = exe_of(pid.value)
                            if wanted is not None and exe not in wanted:
                                continue     # not bound: never even resolve the meter
                            meter = c_void_p()
                            miid = _guid(IID_IAudioMeterInformation)
                            if _vcall(ctl, 0, c_void_p, POINTER(c_void_p))(
                                    ctl, ctypes.cast(byref(miid), c_void_p), byref(meter)) or not meter:
                                continue
                            try:
                                peak = c_float(0.0)
                                _vcall(meter, 3, POINTER(c_float))(meter, byref(peak))
                                out.append(AudioHit(pid=pid.value, exe=exe, peak=float(peak.value)))
                            finally:
                                _vcall(meter, 2)(meter)
                        finally:
                            _vcall(ctl, 2)(ctl)
                finally:
                    _vcall(sess, 2)(sess)
            finally:
                _vcall(mgr, 2)(mgr)
        finally:
            _vcall(dev, 2)(dev)
    finally:
        _vcall(enum, 2)(enum)
    return out


def list_processes() -> list[dict]:
    """Running processes, for the app's "pick a running app" list. Never called
    from the poll loop - only by the UI."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    snap = k32.CreateToolhelp32Snapshot(0x2, 0)     # TH32CS_SNAPPROCESS
    if snap == -1:
        return []
    out: dict[str, dict] = {}
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            name = e.szExeFile.lower()
            out.setdefault(name, {"exe": name, "pid": e.th32ProcessID, "count": 0})
            out[name]["count"] += 1
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    fg = foreground_window()
    # an open session with a flat meter is not playing anything
    loud = {a.exe for a in audio_sessions(None) if a.peak > 0.0}
    for name, p in out.items():
        p["foreground"] = bool(fg and fg.exe == name)
        p["playing_audio"] = name in loud
        p["title"] = fg.title if (fg and fg.exe == name) else ""
        p["path"] = exe_path_of(p["pid"])
        p["name"] = app_name(name, p["path"])
    # most useful first: what you are looking at, then what is making noise
    return sorted(out.values(),
                  key=lambda p: (not p["foreground"], not p["playing_audio"], p["name"].lower()))
