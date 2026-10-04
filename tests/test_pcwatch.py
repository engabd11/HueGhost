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


# -- several apps in one source, and "any game" -----------------------------------

BROWSERS = {"id": "br", "exe": "chrome.exe,msedge.exe", "detect": "either", "mode": "video"}
ANY_GAME = {"id": "g", "exe": "@games", "detect": "fullscreen", "mode": "games"}
STEAM = r"D:\SteamLibrary\steamapps\common\ELDEN RING\Game\eldenring.exe"


def game_fg(exe="eldenring.exe", path=STEAM, fullscreen=True, exclusive=False, pid=300, cls=""):
    return Foreground(pid=pid, exe=exe, title="", fullscreen=fullscreen, monitor_id=MON,
                      cls=cls, path=path, exclusive=exclusive)


def test_one_source_can_stand_for_several_apps():
    for exe in ("chrome.exe", "msedge.exe"):
        p = probe()
        p.observe([BROWSERS], loud(exe), None, 0.0)
        assert p.observe([BROWSERS], loud(exe), None, 1.5)["br"].playing is True
        p = probe()
        p.observe([BROWSERS], [], fg(exe), 0.0)
        assert p.observe([BROWSERS], [], fg(exe), 1.5)["br"].playing is True
    p = probe()
    p.observe([BROWSERS], loud("firefox.exe"), fg("firefox.exe"), 0.0)
    assert p.observe([BROWSERS], loud("firefox.exe"), fg("firefox.exe"), 1.5)["br"].playing is False


def test_binding_exes_splits_and_tidies():
    from hueghost.pcwatch import binding_exes
    assert binding_exes({"exe": " Chrome.exe, msedge.exe ,,"}) == {"chrome.exe", "msedge.exe"}
    assert binding_exes("") == set()


def test_any_game_follows_a_game_from_a_library():
    p = probe()
    p.observe([ANY_GAME], [], game_fg(), 0.0)
    hit = p.observe([ANY_GAME], [], game_fg(), 1.5)["g"]
    assert hit.playing is True and hit.monitor_id == MON


def test_any_game_is_not_any_fullscreen_window():
    p = probe()
    browser = game_fg(exe="chrome.exe", path=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    p.observe([ANY_GAME], [], browser, 0.0)
    assert p.observe([ANY_GAME], [], browser, 1.5)["g"].playing is False


def test_any_game_trusts_exclusive_fullscreen_from_anywhere():
    p = probe()
    bnet = game_fg(exe="wow.exe", path=r"C:\Program Files (x86)\World of Warcraft\_retail_\Wow.exe",
                   fullscreen=False, exclusive=True)
    p.observe([ANY_GAME], [], bnet, 0.0)
    assert p.observe([ANY_GAME], [], bnet, 1.5)["g"].playing is True


def test_any_game_needs_the_game_full_screen():
    p = probe()
    p.observe([ANY_GAME], [], game_fg(fullscreen=False), 0.0)
    assert p.observe([ANY_GAME], [], game_fg(fullscreen=False), 1.5)["g"].playing is False


def test_any_game_ignores_the_shell_and_our_own_windows():
    p = probe()
    desk = game_fg(exe="explorer.exe", exclusive=True, cls="progman")
    p.observe([ANY_GAME], [], desk, 0.0)
    assert p.observe([ANY_GAME], [], desk, 1.5)["g"].playing is False
    p = probe()
    p.ignore_pids = {300}
    p.observe([ANY_GAME], [], game_fg(), 0.0)
    assert p.observe([ANY_GAME], [], game_fg(), 1.5)["g"].playing is False


def test_any_game_by_sound_counts_only_games():
    b = dict(ANY_GAME, detect="audio")
    p = probe()
    game = [AudioHit(pid=1, exe="eldenring.exe", peak=0.5, path=STEAM)]
    p.observe([b], game, None, 0.0)
    assert p.observe([b], game, None, 1.5)["g"].playing is True
    p = probe()
    music = [AudioHit(pid=2, exe="spotify.exe", peak=0.5, path=r"C:\Users\me\AppData\Roaming\Spotify\Spotify.exe")]
    p.observe([b], music, None, 0.0)
    assert p.observe([b], music, None, 1.5)["g"].playing is False


def test_game_library_paths():
    from hueghost.pcwatch import is_game_path
    assert is_game_path(STEAM)
    assert is_game_path(r"C:\Program Files\Epic Games\Fortnite\FortniteClient-Win64-Shipping.exe")
    assert is_game_path(r"C:\XboxGames\Forza Horizon 5\Content\ForzaHorizon5.exe")
    assert is_game_path("C:/GOG Games/Witcher 3/bin/x64/witcher3.exe")
    assert not is_game_path(r"C:\Program Files (x86)\Steam\steam.exe")
    assert not is_game_path(r"C:\Program Files (x86)\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe")
    assert not is_game_path(r"C:\Riot Games\Riot Client\RiotClientServices.exe")
    assert not is_game_path("")


# -- launchers, and games installed anywhere ---------------------------------------

STEAM_SRC = {"id": "st", "exe": "steam.exe", "detect": "either", "mode": "games"}
GOG_CP = "D:/GOG/Cyberpunk 2077/bin/x64/Cyberpunk2077.exe"


def test_a_launcher_source_follows_the_games_it_starts():
    # the game sits outside every library, but Steam started it
    odd = game_fg(exe="mygame.exe", path="E:/Games/MyGame/mygame.exe")
    odd = Foreground(**dict(odd.__dict__, launcher="steam.exe"))
    p = probe()
    p.observe([STEAM_SRC], [], odd, 0.0)
    assert p.observe([STEAM_SRC], [], odd, 1.5)["st"].playing is True


def test_a_launcher_source_follows_its_library_by_sound():
    b = dict(STEAM_SRC, detect="audio")
    p = probe()
    s = [AudioHit(pid=1, exe="eldenring.exe", peak=0.5, path=STEAM)]
    p.observe([b], s, None, 0.0)
    assert p.observe([b], s, None, 1.5)["st"].playing is True


def test_the_launcher_itself_is_not_a_game():
    p = probe()
    # Steam's own window, its sound, and its overlay
    ui = Foreground(pid=5, exe="steamwebhelper.exe", fullscreen=True, monitor_id=MON,
                    path="C:/Program Files (x86)/Steam/bin/cef/cef.win64/steamwebhelper.exe", launcher="steam.exe")
    snd = [AudioHit(pid=6, exe="steam.exe", peak=0.5, path="C:/Program Files (x86)/Steam/steam.exe")]
    p.observe([STEAM_SRC], snd, ui, 0.0)
    assert p.observe([STEAM_SRC], snd, ui, 1.5)["st"].playing is False


def test_a_launcher_source_ignores_other_launchers_games():
    p = probe()
    epic = game_fg(exe="fortnite.exe", path="C:/Program Files/Epic Games/Fortnite/fortnite.exe")
    p.observe([STEAM_SRC], [], epic, 0.0)
    assert p.observe([STEAM_SRC], [], epic, 1.5)["st"].playing is False


def test_a_browser_a_launcher_opened_is_not_a_game():
    p = probe()
    br = Foreground(pid=7, exe="chrome.exe", fullscreen=True, monitor_id=MON,
                    path="C:/Program Files/Google/Chrome/Application/chrome.exe", launcher="steam.exe")
    p.observe([ANY_GAME, STEAM_SRC], [], br, 0.0)
    hits = p.observe([ANY_GAME, STEAM_SRC], [], br, 1.5)
    assert hits["g"].playing is False and hits["st"].playing is False


def test_any_game_follows_a_game_a_launcher_started():
    p = probe()
    g = Foreground(pid=8, exe="gta5.exe", fullscreen=True, monitor_id=MON,
                   path="D:/Rockstar/Grand Theft Auto V/GTA5.exe", launcher="epicgameslauncher.exe")
    p.observe([ANY_GAME], [], g, 0.0)
    assert p.observe([ANY_GAME], [], g, 1.5)["g"].playing is True


def test_any_game_follows_what_windows_lists_as_a_game(monkeypatch):
    from hueghost import pcwatch
    monkeypatch.setattr(pcwatch.CATALOG, "games", {pcwatch._norm(GOG_CP)})
    p = probe()
    cp = game_fg(exe="cyberpunk2077.exe", path=GOG_CP)
    p.observe([ANY_GAME], [], cp, 0.0)
    assert p.observe([ANY_GAME], [], cp, 1.5)["g"].playing is True


def test_a_steam_library_on_another_drive(monkeypatch):
    from hueghost import pcwatch
    monkeypatch.setattr(pcwatch.CATALOG, "roots", {"steam.exe": {"x:/library/".replace("/", "\\")}})
    assert pcwatch.is_game_path("X:/Library/Some Game/game.exe", {"steam.exe"})
    assert pcwatch.is_game_path("X:/Library/Some Game/game.exe")
    assert not pcwatch.is_game_path("X:/Library/Some Game/game.exe", {"galaxyclient.exe"})


def test_launcher_ancestry():
    from hueghost.pcwatch import launcher_in
    procs = {1: (0, "explorer.exe"), 2: (1, "steam.exe"), 3: (2, "gamelauncher.exe"), 4: (3, "game.exe"),
             9: (99, "orphan.exe"), 10: (10, "loop.exe")}
    assert launcher_in(4, procs) == "steam.exe"
    assert launcher_in(3, procs) == "steam.exe"
    assert launcher_in(2, procs) == ""          # Steam itself was started by explorer
    assert launcher_in(9, procs) == ""          # parent gone
    assert launcher_in(10, procs) == ""         # a cycle does not hang


def test_a_protected_process_still_has_a_name(monkeypatch):
    from hueghost import pcwatch
    monkeypatch.setattr(pcwatch, "_query_exe", lambda pid: "")
    monkeypatch.setattr(pcwatch, "_snapshot", lambda: {4242: (1, "protectedgame.exe")})
    pcwatch._exe_cache.pop(4242, None)
    assert pcwatch.exe_of(4242) == "protectedgame.exe"
    pcwatch._exe_cache.pop(4242, None)
