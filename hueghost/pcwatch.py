"""Is an app on this PC playing something?

Two signals, both cheap and both scoped to the executables the user actually
bound - there is no scan of every process, and no guessing about what is a
"game" or a "video app":

  sound      a WASAPI session belonging to that exe is above the noise floor
  fullscreen that exe owns the foreground window and it covers a whole monitor

A binding's ``exe`` can list several executables, comma separated
(``chrome.exe,msedge.exe``): one source for every browser, or a game and its
launcher. The exe ``*`` means *any app*: any sound, any window filling a
display. ``huesync.exe`` means the same for the window signal - Hue Sync's own
window is never a fullscreen video, and binding it is how people say "this PC"
(its loopback capture already makes its sound session mirror the whole PC).

The exe ``@games`` means *any game*: an app installed in a game library
(Steam, Epic, GOG, Xbox, EA, Ubisoft, Riot) or one Windows reports running
exclusive fullscreen Direct3D. That is also what a game started by Sunshine for
a Moonlight client looks like, so streamed games need no setup of their own.
A game is also anything Windows itself lists as one (the Game Bar's own list,
wherever the game is installed), and anything a game launcher started.

A launcher's exe (``steam.exe``, ``galaxyclient.exe``, ...) stands for the games
it starts, not for the launcher: the Steam client's window and sound belong to
steamwebhelper.exe, and a Steam game is a process of its own, so ``steam.exe``
taken literally never plays anything.

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
GAMES = "@games"
# where the launchers install games; the launchers themselves live elsewhere,
# so a store front left open full screen is never "a game"
GAME_DIRS = ("\\steamapps\\common\\", "\\epic games\\", "\\gog galaxy\\games\\",
             "\\gog games\\", "\\xboxgames\\", "\\ea games\\", "\\origin games\\",
             "\\ubisoft game launcher\\games\\", "\\riot games\\")
# ... except the two launchers that install inside their own library folder
LAUNCHER_DIRS = ("\\epic games\\launcher\\", "\\riot games\\riot client\\")
# windows that fill a display without anything playing: the desktop, the
# taskbar, the lock screen, start/search - never "a fullscreen video"
SHELL_CLASSES = {"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd"}
# a launcher's exe -> where it installs its games. Its games are also the
# processes it starts, so a game installed anywhere still counts.
LAUNCHERS: dict[str, tuple[str, ...]] = {
    "steam.exe": ("\\steamapps\\common\\",),
    "epicgameslauncher.exe": ("\\epic games\\",),
    "galaxyclient.exe": ("\\gog galaxy\\games\\", "\\gog games\\"),
    "eadesktop.exe": ("\\ea games\\",),
    "origin.exe": ("\\origin games\\",),
    "upc.exe": ("\\ubisoft game launcher\\games\\",),
    "ubisoftconnect.exe": ("\\ubisoft game launcher\\games\\",),
    "riotclientservices.exe": ("\\riot games\\",),
    "battle.net.exe": (),
    "xboxpcapp.exe": ("\\xboxgames\\",),
    "gamelaunchhelper.exe": ("\\xboxgames\\",),
}
# what a launcher runs that is not a game: its own UI, overlay, crash
# reporters - and the apps a link in it opens
NOT_GAMES = {"steamwebhelper.exe", "gameoverlayui.exe", "gameoverlayui64.exe", "steamerrorreporter.exe",
             "steamerrorreporter64.exe", "steamservice.exe", "steam_monitor.exe", "epicwebhelper.exe",
             "epiconlineservicesuserhelper.exe", "unrealcefsubprocess.exe", "crashreportclient.exe",
             "galaxyclient helper.exe", "galaxycommunication.exe", "gogcrashreporter.exe",
             "eabackgroundservice.exe", "eaconnect_microsoft.exe", "uplaywebcore.exe", "upc_service.exe",
             "riotclientux.exe", "riotclientuxrender.exe", "riotclientcrashhandler.exe",
             "agent.exe", "battle.net helper.exe", "blizzarderror.exe",
             "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe",
             "msedgewebview2.exe", "cmd.exe", "conhost.exe", "powershell.exe", "werfault.exe"}
SHELL_EXES = {"explorer.exe", "lockapp.exe", "searchhost.exe", "searchapp.exe",
              "startmenuexperiencehost.exe", "shellexperiencehost.exe", "textinputhost.exe",
              "hueghost.exe"}


def binding_exes(b_or_exe) -> set[str]:
    """The executables one binding stands for: ``exe`` split on commas."""
    raw = b_or_exe.get("exe") if isinstance(b_or_exe, dict) else b_or_exe
    return {e.strip().lower() for e in str(raw or "").split(",") if e.strip()}


def any_app(exes, signal: str) -> bool:
    """Whether a binding's exe(s) stand for every app, for this signal."""
    exes = binding_exes(exes) if isinstance(exes, str) else set(exes)
    return ANY_APP in exes or (signal == "window" and "huesync.exe" in exes)


def _norm(path: str) -> str:
    return (path or "").replace("/", "\\").lower()


def is_game_path(path: str, launchers: set[str] | None = None) -> bool:
    """Whether an executable is a game: it lives in a game library, or Windows
    lists it as a game. ``launchers`` narrows that to those launchers' own
    libraries (a "Steam" source is Steam's games, not every game)."""
    p = _norm(path)
    if not p or any(d in p for d in LAUNCHER_DIRS):
        return False
    if launchers is None:
        return (any(d in p for d in GAME_DIRS) or p in CATALOG.games
                or any(p.startswith(r) for roots in CATALOG.roots.values() for r in roots))
    return any(any(d in p for d in LAUNCHERS.get(l, ())) or any(p.startswith(r) for r in CATALOG.roots.get(l, ()))
               for l in launchers)


def is_game(exe: str, path: str, launcher: str = "", launchers: set[str] | None = None) -> bool:
    """Whether a process is a game - of any launcher (``launchers`` None) or
    of the given ones. A launcher's own processes, and the apps a link in it
    opens, never are."""
    exe = (exe or "").lower()
    if exe in NOT_GAMES or exe in LAUNCHERS:
        return False
    if launchers is None:
        return bool(launcher) or is_game_path(path)
    return launcher in launchers or is_game_path(path, launchers)


class GameCatalog:
    """Where this PC's games are, from Windows and the launchers themselves.

    ``games``: full paths Windows' Game Bar knows as games
    (``HKCU\\System\\GameConfigStore\\Children``) - it notices a game the
    first time one runs, wherever it is installed. ``roots``: install folders
    the launchers record, per launcher exe (Steam's libraries on other drives,
    Epic and GOG games in folders of the user's choosing).

    Read lazily and rarely: ``refresh`` re-reads at most every ``every_s``,
    and the registry list only when its key actually changed."""

    def __init__(self, every_s: float = 30.0):
        self.every_s = float(every_s)
        self.games: set[str] = set()
        self.roots: dict[str, set[str]] = {}
        self._next = 0.0
        self._stamp = None

    def refresh(self, now: float | None = None) -> None:
        if sys.platform != "win32":
            return
        now = time.monotonic() if now is None else now
        if now < self._next:
            return
        self._next = now + self.every_s
        try:
            stamp = _gcs_stamp()
            if stamp != self._stamp:
                self._stamp = stamp
                self.games = _gcs_games()
            self.roots = _launcher_roots()
        except Exception:                   # a catalog is a nicety; never stop the poll
            log.debug("game catalog refresh failed", exc_info=True)


CATALOG = GameCatalog()


@dataclass(frozen=True)
class AudioHit:
    pid: int
    exe: str                 # basename, lower case
    peak: float
    path: str = ""           # full path; only read when an "any game" source needs it
    launcher: str = ""       # the game launcher that started it, if any (same)


@dataclass(frozen=True)
class Foreground:
    pid: int
    exe: str                 # basename, lower case
    title: str = ""
    fullscreen: bool = False
    monitor_id: str = ""     # the display it is on, as the Hue Sync app names it
    cls: str = ""            # window class, lower case
    path: str = ""           # full path of the exe; only read for "any game"
    exclusive: bool = False  # Windows reports exclusive fullscreen Direct3D
    launcher: str = ""       # the game launcher that started it, if any

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
            exes = binding_exes(b)
            detect = b.get("detect") or "audio"
            hit = self.hits.get(b["id"]) or Hit()
            any_sound, any_window = any_app(exes, "audio"), any_app(exes, "window")
            games = GAMES in exes
            # a launcher stands for its games; the launcher itself never plays
            launchers = exes & LAUNCHERS.keys()

            def game(x) -> bool:
                return bool((games and is_game(x.exe, x.path, x.launcher))
                            or (launchers and is_game(x.exe, x.path, x.launcher, launchers)))

            loud = max((s.peak for s in sessions
                        if (any_sound or (s.exe in exes and s.exe not in launchers) or game(s))
                        and s.pid not in self.ignore_pids
                        and s.pid != os.getpid()), default=0.0)
            by_audio = loud > self.peak
            fg_game = bool(fg and not fg.shell
                           and ((games and fg.exclusive) or game(fg)))
            fg_mine = bool(fg and fg.pid not in self.ignore_pids
                           and ((fg.exe in exes and fg.exe not in launchers)
                                or (any_window and not fg.shell) or fg_game))
            by_window = bool(fg_mine and (fg.fullscreen or (fg_game and fg.exclusive)))
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
        wanted: set[str] | None = set().union(*(binding_exes(b) for b in bindings)) if bindings else set()
        # "any game" and a launcher's games both need to know what a game is
        games = GAMES in wanted or bool(wanted & LAUNCHERS.keys())
        if any_app(wanted, "audio") or games:
            wanted = None                   # every app's sound counts (or might be a game)
        if games:
            CATALOG.refresh()               # cheap: re-reads at most every 30 s
        need_audio = any((b.get("detect") or "audio") in ("audio", "either") for b in bindings)
        need_fg = any((b.get("detect") or "audio") in ("fullscreen", "either") for b in bindings)
        sessions = audio_sessions(wanted, paths=games) if need_audio else []
        fg = foreground_window(games=games) if need_fg else None
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
    # a protected process (some anti-cheat games) refuses OpenProcess; the
    # process list still has its name
    name = _query_exe(pid) or _snapshot().get(pid, (0, ""))[1]
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


_path_cache: dict[int, tuple[str, float]] = {}


def path_of(pid: int) -> str:
    """``exe_path_of``, cached like ``exe_of`` - the poll loop asks every second."""
    if pid <= 0:
        return ""
    now = time.monotonic()
    hit = _path_cache.get(pid)
    if hit and now - hit[1] < _EXE_TTL_S:
        return hit[0]
    path = exe_path_of(pid)
    _path_cache[pid] = (path, now)
    if len(_path_cache) > 512:
        for k in [k for k, v in _path_cache.items() if now - v[1] > _EXE_TTL_S]:
            _path_cache.pop(k, None)
    return path


_snap: tuple[float, dict[int, tuple[int, str]]] = (-1e9, {})
_SNAP_TTL_S = 2.0
_launcher_cache: dict[int, tuple[str, float]] = {}
_MAX_DEPTH = 8


def _snapshot() -> dict[int, tuple[int, str]]:
    """{pid: (parent pid, exe)} for every process, from one Toolhelp snapshot.
    Only taken when a pid not seen in the last minute needs looking up, and
    shared by every lookup within two seconds of it."""
    global _snap
    now = time.monotonic()
    if now - _snap[0] < _SNAP_TTL_S:
        return _snap[1]
    procs = _read_snapshot()
    _snap = (now, procs)
    return procs


def _read_snapshot() -> dict[int, tuple[int, str]]:
    if sys.platform != "win32":
        return {}
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    snap = k32.CreateToolhelp32Snapshot(0x2, 0)     # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return {}
    out: dict[int, tuple[int, str]] = {}
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = (int(e.th32ParentProcessID), e.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def launcher_in(pid: int, procs: dict[int, tuple[int, str]]) -> str:
    """Pure: the game launcher ``pid`` descends from, or "". Steam starts a game
    directly or through the game's own launcher, so a few levels are walked."""
    seen = {pid}
    cur = procs.get(pid, (0, ""))[0]
    for _ in range(_MAX_DEPTH):
        if cur <= 0 or cur in seen or cur not in procs:
            return ""
        seen.add(cur)
        parent, exe = procs[cur]
        if exe in LAUNCHERS:
            return exe
        cur = parent
    return ""


def launcher_of(pid: int) -> str:
    """``launcher_in`` against the live process list, cached per pid."""
    if pid <= 0:
        return ""
    now = time.monotonic()
    hit = _launcher_cache.get(pid)
    if hit and now - hit[1] < _EXE_TTL_S:
        return hit[0]
    name = launcher_in(pid, _snapshot())
    _launcher_cache[pid] = (name, now)
    if len(_launcher_cache) > 512:
        for k in [k for k, v in _launcher_cache.items() if now - v[1] > _EXE_TTL_S]:
            _launcher_cache.pop(k, None)
    return name


# -- where the games are ---------------------------------------------------------
_GCS_KEY = r"System\GameConfigStore\Children"


def _gcs_stamp():
    """Last-write time of Windows' game list: re-read it only when it moved."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GCS_KEY) as k:
            return winreg.QueryInfoKey(k)[2]
    except OSError:
        return None


def _gcs_games() -> set[str]:
    """Every executable Windows' Game Bar has recognised as a game."""
    import winreg
    out: set[str] = set()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GCS_KEY) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                try:
                    with winreg.OpenKey(k, winreg.EnumKey(k, i)) as c:
                        path = winreg.QueryValueEx(c, "MatchedExeFullPath")[0]
                except OSError:
                    continue
                if path:
                    out.add(_norm(str(path)))
    except OSError:
        pass
    return out


def _dir(path: str) -> str:
    p = _norm(path).rstrip("\\")
    return p + "\\" if p else ""


def _launcher_roots() -> dict[str, set[str]]:
    """Install folders each launcher recorded, for libraries outside the usual
    places: a Steam library on another drive, an Epic or GOG game wherever the
    user put it."""
    import glob
    import json
    import re
    import winreg

    out: dict[str, set[str]] = {}

    def reg(hive, key, value):
        try:
            with winreg.OpenKey(hive, key) as k:
                return str(winreg.QueryValueEx(k, value)[0] or "")
        except OSError:
            return ""

    steam = reg(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath")
    if steam:
        roots = {_dir(steam) + "steamapps\\common\\"}
        try:
            with open(steam.replace("/", "\\") + r"\steamapps\libraryfolders.vdf", encoding="utf-8",
                      errors="replace") as f:
                for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                    roots.add(_dir(m.group(1).replace("\\\\", "\\")) + "steamapps\\common\\")
        except OSError:
            pass
        out["steam.exe"] = roots
    epic = set()
    for item in glob.glob(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests\*.item"):
        try:
            with open(item, encoding="utf-8", errors="replace") as f:
                loc = json.load(f).get("InstallLocation") or ""
        except (OSError, ValueError):
            continue
        if loc:
            epic.add(_dir(loc))
    if epic:
        out["epicgameslauncher.exe"] = epic
    gog = set()
    for base in (r"SOFTWARE\WOW6432Node\GOG.com\Games", r"SOFTWARE\GOG.com\Games"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    p = reg(winreg.HKEY_LOCAL_MACHINE, base + "\\" + winreg.EnumKey(k, i), "path")
                    if p:
                        gog.add(_dir(p))
        except OSError:
            continue
    if gog:
        out["galaxyclient.exe"] = gog
    return out


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


def foreground_window(games: bool = False) -> Foreground | None:
    """The focused window, its process, and whether it covers a whole display.
    ``games`` also reads its path, the launcher that started it and the
    exclusive-fullscreen state, which only "any game" and launcher sources need."""
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
                      fullscreen=full, monitor_id=monitor_id, cls=cls.value.lower(),
                      path=path_of(pid.value) if games else "",
                      exclusive=exclusive_fullscreen() if games else False,
                      launcher=launcher_of(pid.value) if games else "")


QUNS_RUNNING_D3D_FULL_SCREEN = 3


def exclusive_fullscreen() -> bool:
    """Whether Windows says a Direct3D app holds the screen exclusively - the
    one signal that finds a game installed outside every known library."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
        return state.value == QUNS_RUNNING_D3D_FULL_SCREEN
    except Exception:
        return False


def audio_sessions(wanted: set[str] | None = None, paths: bool = False) -> list[AudioHit]:
    """Processes currently playing sound on the default output device.

    Each session hands us its pid directly, so no process enumeration is
    needed; ``wanted`` drops the rest before the pid is even resolved."""
    if sys.platform != "win32":
        return []
    try:
        out = _audio_sessions(wanted)
    except Exception as e:                  # a COM failure must not stop the daemon
        log.debug("audio session enumeration failed: %s", e, exc_info=True)
        return []
    if paths:
        # a silent session is never a game playing: only look up the loud ones
        out = [AudioHit(pid=a.pid, exe=a.exe, peak=a.peak, path=path_of(a.pid), launcher=launcher_of(a.pid))
               if a.peak > 0.0 else a for a in out]
    return out


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
