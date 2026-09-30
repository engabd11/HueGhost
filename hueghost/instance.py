"""One Hue Ghost per user session.

Two copies of the daemon fight over everything that exists once on a PC: the
Hue Sync app, the ghost display and the mpv IPC pipe. One copy's "quit" lands
in the other's mpv, which reads it as the user closing the window and switches
its sync off; Home Assistant (or the CLI) talks to whichever copy owns the
control port while the window you look at belongs to the other, so the two
disagree about whether sync is on. Double-clicking the app while autostart
already runs it in the tray is all it takes - so a second copy is refused.

Windows: a named mutex marks the running copy and a named event lets a second
launch ask it to show its window. Elsewhere an flock'ed file does the first
job. ``HUEGHOST_INSTANCE`` names a different lock, for a deliberate side-by-side
dev instance (it needs its own control port and mpv pipe as well).
"""
from __future__ import annotations

import logging
import os
import sys
import threading

log = logging.getLogger("hue-ghost")

_ERROR_ALREADY_EXISTS = 183
_WAIT_OBJECT_0 = 0


_K32 = None


def _k32():
    """kernel32 with the signatures set: without argtypes ctypes passes a
    HANDLE as a C int, which truncates on 64-bit."""
    global _K32
    if _K32 is None:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        H = wintypes.HANDLE
        for fn, res, args in (
            ("CreateMutexW", H, (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)),
            ("CreateEventW", H, (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)),
            ("OpenEventW", H, (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)),
            ("SetEvent", wintypes.BOOL, (H,)),
            ("CloseHandle", wintypes.BOOL, (H,)),
            ("WaitForSingleObject", wintypes.DWORD, (H, wintypes.DWORD)),
        ):
            f = getattr(k, fn)
            f.restype, f.argtypes = res, args
        _K32 = k
    return _K32


def _name() -> str:
    return os.environ.get("HUEGHOST_INSTANCE") or "HueGhost"


class Instance:
    """The lock this process holds. ``show_requested`` is set when another
    launch asked for the window; the GUI polls it from its own thread."""

    def __init__(self) -> None:
        self.show_requested = threading.Event()
        self._mutex = None
        self._event = None
        self._fh = None
        self._stop = threading.Event()

    def release(self) -> None:
        """Before a relaunch: the new process must find the lock free."""
        self._stop.set()
        if sys.platform == "win32":
            k32 = _k32()
            for h in (self._mutex, self._event):
                if h:
                    k32.CloseHandle(h)
        elif self._fh is not None:
            self._fh.close()
        self._mutex = self._event = self._fh = None

    def _listen(self) -> None:
        k32 = _k32()
        while not self._stop.is_set():
            h = self._event
            if not h:
                return
            if k32.WaitForSingleObject(h, 1000) == _WAIT_OBJECT_0:
                self.show_requested.set()


def acquire() -> Instance | None:
    """The lock, or None when another copy already runs. Never raises: a lock
    that cannot be made at all must not stop the app from starting."""
    inst = Instance()
    try:
        if sys.platform == "win32":
            import ctypes
            k32 = _k32()
            h = k32.CreateMutexW(None, True, "Local\\%s.instance" % _name())
            if h and ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
                k32.CloseHandle(h)
                return None
            inst._mutex = h
            inst._event = k32.CreateEventW(None, False, False, "Local\\%s.show" % _name())
            if inst._event:
                threading.Thread(target=inst._listen, name="hue-ghost-instance", daemon=True).start()
        else:
            import fcntl
            from .config import app_data_dir
            d = app_data_dir()
            os.makedirs(d, exist_ok=True)
            fh = open(os.path.join(d, "%s.lock" % _name()), "a")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return None
            inst._fh = fh
    except Exception as e:      # pragma: no cover - platform oddities
        log.warning("single-instance lock unavailable: %s", e)
    return inst


def ask_running_to_show() -> bool:
    """Tell the copy that holds the lock to show its window."""
    if sys.platform != "win32":
        return False
    try:
        k32 = _k32()
        EVENT_MODIFY_STATE = 0x0002
        h = k32.OpenEventW(EVENT_MODIFY_STATE, False, "Local\\%s.show" % _name())
        if not h:
            return False
        ok = bool(k32.SetEvent(h))
        k32.CloseHandle(h)
        return ok
    except Exception:
        return False
