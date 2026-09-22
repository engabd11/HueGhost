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
