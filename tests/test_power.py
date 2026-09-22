"""The Windows power/display helpers must be safe to call anywhere: real
calls on Windows, quiet no-ops elsewhere."""
import sys

from hueghost import winutil


def test_helpers_are_safe_everywhere():
    if sys.platform == "win32":
        assert winutil.keep_awake(True) is True
        assert winutil.keep_awake(False) is True
        assert isinstance(winutil.wake_display(), bool)
        assert winutil.desktop_locked() in (True, False)
    else:
        assert winutil.keep_awake(True) is False
        assert winutil.wake_display() is False
        assert winutil.desktop_locked() is None
