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
        idle = winutil.idle_seconds()
        assert idle is None or idle >= 0.0
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            assert winutil.kill_on_exit(p) is True
        finally:
            p.kill()
            p.wait()
    else:
        assert winutil.keep_awake(True) is False
        assert winutil.wake_display() is False
        assert winutil.desktop_locked() is None
        assert winutil.idle_seconds() is None
        assert winutil.kill_on_exit(object()) is False


def test_audio_and_display_enumeration_survive_a_pc_with_no_sound_card():
    """A CI runner (and a headless PC) has no default audio endpoint at all.

    The COM call fails with "element not found", which is a normal state here -
    it must read as "no devices", not raise out of the engine's constructor,
    which is exactly what it used to do."""
    from hueghost import winutil

    assert isinstance(winutil.default_audio_output_id(), str)
    outs = winutil.list_audio_outputs()
    assert isinstance(outs, list)
    for a in outs:                       # whatever this machine has, it is coherent
        assert a.id.endswith(a.guid) and a.mpv_device == "wasapi/" + a.guid
    assert winutil.mpv_audio_device("") == ""
    assert winutil.mpv_audio_device("nonsense") == ""
    assert isinstance(winutil.list_displays(), list)


def test_pc_detection_never_raises_without_a_desktop():
    from hueghost import pcwatch

    assert isinstance(pcwatch.audio_sessions(set()), list)
    assert isinstance(pcwatch.list_processes(), list)
    fg = pcwatch.foreground_window()
    assert fg is None or isinstance(fg.exe, str)
