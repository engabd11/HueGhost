"""Detecting an app playing on this PC: the pure half.

``PcProbe.observe`` takes the two readings and the clock, so none of this
touches COM or a real window - the same trick that lets the daemon tests run
on a virtual clock.
"""
from hueghost.pcwatch import AudioHit, Foreground, PcProbe

VIDEO = {"id": "ff", "exe": "firefox.exe", "detect": "audio", "mode": "video"}
GAME = {"id": "er", "exe": "eldenring.exe", "detect": "fullscreen", "mode": "games"}
EITHER = {"id": "vlc", "exe": "vlc.exe", "detect": "either", "mode": "video"}

MON = r"MONITOR\DELA212\{4d36e96e}\0001"


def probe(**kw):
    return PcProbe(peak=0.002, hold_s=3.0, confirm_s=1.0, **kw)


def loud(exe, peak=0.5, pid=100):
    return [AudioHit(pid=pid, exe=exe, peak=peak)]


def fg(exe, fullscreen=True, pid=200, title=""):
    return Foreground(pid=pid, exe=exe, title=title, fullscreen=fullscreen, monitor_id=MON)


def test_sound_has_to_last_before_it_counts():
    p = probe()
    # a notification chime is not a film
    assert p.observe([VIDEO], loud("firefox.exe"), None, 0.0)["ff"].playing is False
    assert p.observe([VIDEO], loud("firefox.exe"), None, 0.5)["ff"].playing is False
    assert p.observe([VIDEO], loud("firefox.exe"), None, 1.0)["ff"].playing is True


def test_a_quiet_moment_does_not_stop_the_lights():
    p = probe()
    p.observe([VIDEO], loud("firefox.exe"), None, 0.0)
    assert p.observe([VIDEO], loud("firefox.exe"), None, 1.5)["ff"].playing is True
    # dialogue gap: silent for 2 s, still playing
    assert p.observe([VIDEO], [], None, 3.5)["ff"].playing is True
    # gone for 4 s: stopped
    assert p.observe([VIDEO], [], None, 5.6)["ff"].playing is False


def test_the_noise_floor_is_not_playback():
    p = probe()
    p.observe([VIDEO], loud("firefox.exe", peak=0.0), None, 0.0)
    assert p.observe([VIDEO], loud("firefox.exe", peak=0.0), None, 2.0)["ff"].playing is False


def test_a_game_needs_foreground_and_fullscreen():
    p = probe()
    p.observe([GAME], [], fg("eldenring.exe", fullscreen=False), 0.0)
    assert p.observe([GAME], [], fg("eldenring.exe", fullscreen=False), 2.0)["er"].playing is False
    p = probe()
    p.observe([GAME], [], fg("eldenring.exe"), 0.0)
    hit = p.observe([GAME], [], fg("eldenring.exe"), 2.0)["er"]
    assert hit.playing is True and hit.monitor_id == MON


def test_a_game_in_the_background_does_not_count_even_when_loud():
    p = probe()
    p.observe([GAME], loud("eldenring.exe"), fg("code.exe"), 0.0)
    assert p.observe([GAME], loud("eldenring.exe"), fg("code.exe"), 2.0)["er"].playing is False


def test_either_fires_on_whichever_signal_arrives():
    p = probe()
    p.observe([EITHER], loud("vlc.exe"), None, 0.0)
    assert p.observe([EITHER], loud("vlc.exe"), None, 2.0)["vlc"].playing is True
    p = probe()
    p.observe([EITHER], [], fg("vlc.exe"), 0.0)
    assert p.observe([EITHER], [], fg("vlc.exe"), 2.0)["vlc"].playing is True


def test_our_own_ghost_is_never_mistaken_for_content():
    # someone who binds mpv.exe must not end up syncing to the ghost we launched
    p = probe()
    p.ignore_pids = {999}
    b = {"id": "mpv", "exe": "mpv.exe", "detect": "either", "mode": "video"}
    sessions = [AudioHit(pid=999, exe="mpv.exe", peak=0.9)]
    p.observe([b], sessions, fg("mpv.exe", pid=999), 0.0)
    assert p.observe([b], sessions, fg("mpv.exe", pid=999), 2.0)["mpv"].playing is False
    # the same app, a different process (one the user started) does count
    p.observe([b], loud("mpv.exe", pid=1000), None, 3.0)
    assert p.observe([b], loud("mpv.exe", pid=1000), None, 5.0)["mpv"].playing is True


def test_an_unbound_app_is_ignored_entirely():
    p = probe()
    out = p.observe([VIDEO], loud("chrome.exe"), fg("chrome.exe"), 2.0)
    assert list(out) == ["ff"] and out["ff"].playing is False


# -- "any app on this PC" (2.10.1) ------------------------------------------------------

ANY_WINDOW = {"id": "any", "exe": "*", "detect": "fullscreen", "mode": "video"}
# how people already said "this PC": Hue Sync's own exe, which never goes fullscreen itself
THIS_PC = {"id": "hs", "exe": "huesync.exe", "detect": "fullscreen", "mode": "video"}


def _playing_after(p, binding, fg_=None, sound=None):
    for t in (0.0, 0.5, 1.0, 1.5):
        hit = p.observe([binding], sound or [], fg_, t)[binding["id"]]
    return hit


def test_any_app_filling_a_screen_counts():
    assert _playing_after(probe(), ANY_WINDOW, fg("chrome.exe", title="YouTube")).playing
    assert _playing_after(probe(), ANY_WINDOW, fg("chrome.exe", fullscreen=False)).playing is False


def test_huesync_exe_means_this_pc_for_the_window_signal():
    """Live 2.9.0: bound as huesync.exe + fullscreen, a video filling the main
    screen never lit anything - it looked for Hue Sync's own window."""
    assert _playing_after(probe(), THIS_PC, fg("vlc.exe", title="film.mkv")).playing
    assert _playing_after(probe(), THIS_PC, fg("vlc.exe", title="film.mkv")).monitor_id == MON


def test_the_desktop_the_taskbar_and_the_lock_screen_are_not_a_film():
    desk = Foreground(pid=300, exe="explorer.exe", fullscreen=True, monitor_id=MON, cls="workerw")
    lock = Foreground(pid=301, exe="lockapp.exe", fullscreen=True, monitor_id=MON)
    shell = Foreground(pid=302, exe="whatever.exe", fullscreen=True, monitor_id=MON, cls="progman")
    for w in (desk, lock, shell):
        assert _playing_after(probe(), ANY_WINDOW, w).playing is False, w


def test_our_own_windows_are_not_a_film():
    import os
    me = Foreground(pid=os.getpid(), exe="python.exe", fullscreen=True, monitor_id=MON)
    assert _playing_after(probe(), ANY_WINDOW, me).playing is False
    p = probe()
    p.ignore_pids = {555}                              # the ghost, or a screen-care cover
    assert _playing_after(p, ANY_WINDOW, fg("mpv.exe", pid=555)).playing is False


def test_any_app_by_sound():
    any_sound = {"id": "snd", "exe": "*", "detect": "audio", "mode": "music"}
    assert _playing_after(probe(), any_sound, sound=loud("spotify.exe")).playing
    p = probe()
    p.ignore_pids = {100}
    assert _playing_after(p, any_sound, sound=loud("mpv.exe", pid=100)).playing is False
