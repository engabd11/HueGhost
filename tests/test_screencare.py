"""Screen care: the pixel shift of the ghost and the black-out of the displays
Hue Sync does not capture, on a virtual clock with scripted input idleness,
fake displays and recorded mpv launches."""
import pytest

from hueghost import screencare as sc
from hueghost.config import Config
from hueghost.screencare import ORBIT, SHIFT_STEP, ScreenCare, cover_args, orbit_offset
from hueghost.winutil import Display

MIN = 60.0

MAIN = Display(r"\\.\DISPLAY1", 0, 3440, 1440, 0, 0, True, monitor_id="MONITOR\\OLED\\1")
SIDE = Display(r"\\.\DISPLAY2", 1, 1920, 1080, 3440, 0, False, monitor_id="MONITOR\\SIDE\\2")
GHOST = Display(r"\\.\DISPLAY3", 2, 1920, 1080, 5360, 0, False, monitor_id="MONITOR\\MTT1337\\3")


class Ghost:
    def __init__(self):
        self.pans = []

    def set_pan(self, x, y):
        self.pans.append((x, y))


class Proc:
    def __init__(self, args):
        self.args = args
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


class Rig:
    def __init__(self, **ghost_cfg):
        self.cfg = Config({"ghost": dict({"screen_name": GHOST.name}, **ghost_cfg)})
        self.idle = 0.0
        self.displays = [MAIN, SIDE, GHOST]
        self.launched: list[Proc] = []
        self.idle_asked = 0

        def idle():
            self.idle_asked += 1
            return self.idle

        def launch(args):
            p = Proc(args)
            self.launched.append(p)
            return p

        self.care = ScreenCare(self.cfg, idle=idle, displays=lambda: list(self.displays), launch=launch)

    def covered_screens(self):
        return sorted(int(a.split("=")[1]) for p in self.launched if not p.terminated
                      for a in p.args if a.startswith("--fs-screen="))


# -- pixel shift ---------------------------------------------------------------------
def test_the_orbit_visits_every_position_once_per_cycle_and_stays_small():
    seen = [orbit_offset(i) for i in range(len(ORBIT))]
    assert seen[0] == (0.0, 0.0)
    assert len(set(seen)) == len(ORBIT)
    assert orbit_offset(len(ORBIT)) == seen[0]
    assert all(abs(x) <= SHIFT_STEP and abs(y) <= SHIFT_STEP for x, y in seen)


def test_the_ghost_picture_moves_every_few_minutes():
    r = Rig(pixel_shift_min=3)
    g = Ghost()
    r.care.tick(0.0, g)
    assert g.pans == []                       # a fresh ghost starts centred
    r.care.tick(2.9 * MIN, g)
    assert g.pans == []
    r.care.tick(3.0 * MIN, g)
    assert g.pans == [orbit_offset(1)]
    r.care.tick(4.0 * MIN, g)
    r.care.tick(6.0 * MIN, g)
    assert g.pans == [orbit_offset(1), orbit_offset(2)]
    assert r.care.status()["pan"] == list(orbit_offset(2))


def test_the_orbit_carries_on_into_the_next_ghost():
    """A night of short episodes must not keep wearing the first positions."""
    r = Rig(pixel_shift_min=1)
    first = Ghost()
    for t in (0.0, 1 * MIN, 2 * MIN):
        r.care.tick(t, first)
    r.care.tick(2.5 * MIN, None)              # episode over, ghost closed
    assert r.care.pan == (0.0, 0.0)
    second = Ghost()
    r.care.tick(3 * MIN, second)
    assert second.pans == [orbit_offset(2)]


def test_switching_the_shift_off_recentres_the_picture():
    r = Rig(pixel_shift_min=1)
    g = Ghost()
    r.care.tick(0.0, g)
    r.care.tick(1 * MIN, g)
    r.cfg.set("ghost.pixel_shift_min", 0)
    r.care.tick(1.1 * MIN, g)
    assert g.pans[-1] == (0.0, 0.0)
    r.care.tick(10 * MIN, g)
    assert len(g.pans) == 2                   # and stays put


def test_a_shorter_interval_applies_without_waiting_out_the_old_one():
    r = Rig(pixel_shift_min=30)
    g = Ghost()
    r.care.tick(0.0, g)
    r.cfg.set("ghost.pixel_shift_min", 1)
    r.care.tick(1.0, g)
    r.care.tick(1.0 + 1 * MIN, g)
    assert g.pans == [orbit_offset(1)]


# -- black out -------------------------------------------------------------------------
def test_blackout_at_zero_never_even_asks_for_idle_time():
    r = Rig(blackout_idle_min=0)
    r.idle = 10 * 3600
    for t in range(5):
        r.care.tick(float(t), Ghost())
    assert r.launched == [] and r.idle_asked == 0


def test_idle_pc_blacks_out_every_display_but_the_ghost_one():
    r = Rig(blackout_idle_min=5)
    g = Ghost()
    r.idle = 4.9 * MIN
    r.care.tick(0.0, g)
    assert r.launched == []
    r.idle = 5 * MIN
    r.care.tick(1.0, g)
    assert r.covered_screens() == [0, 1]      # never 2: that is what the lights come from
    assert r.care.status()["blacked_out"] == sorted([MAIN.name, SIDE.name])
    r.idle = 6 * MIN
    r.care.tick(2.0, g)
    assert len(r.launched) == 2               # covered once, not once per tick


def test_any_input_brings_the_displays_back_and_the_next_idle_stretch_covers_again():
    r = Rig(blackout_idle_min=5)
    g = Ghost()
    r.idle = 5 * MIN
    r.care.tick(0.0, g)
    r.idle = 0.2                              # the mouse moved
    r.care.tick(0.25, g)
    assert all(p.terminated for p in r.launched) and r.care.covers == {}
    r.idle = 5 * MIN
    r.care.tick(300.0, g)
    assert len(r.launched) == 4 and r.covered_screens() == [0, 1]


def test_nothing_is_blacked_out_while_something_plays_on_this_pc():
    """A film in a browser on the OLED while the TV's ghost runs: nobody moves
    the mouse during a film, and the blackout covered the screen being watched."""
    r = Rig(blackout_idle_min=1)
    r.idle = 2 * MIN
    r.care.tick(0.0, Ghost(), watched=True)
    assert r.launched == []
    r.care.tick(0.25, Ghost())
    assert r.covered_screens() == [0, 1]
    r.care.tick(0.5, Ghost(), watched=True)     # it starts while the screens are black
    assert r.care.covers == {}


def test_closing_the_ghost_uncovers():
    r = Rig(blackout_idle_min=1)
    r.idle = 2 * MIN
    r.care.tick(0.0, Ghost())
    assert r.covered_screens() == [0, 1]
    r.care.tick(0.25, None)                   # film over / music / an app on this PC
    assert r.covered_screens() == [] and r.care.covers == {}


def test_the_display_hue_sync_captures_is_spared_too():
    r = Rig(blackout_idle_min=1)
    r.idle = 2 * MIN
    r.care.tick(0.0, Ghost(), spare={MAIN.monitor_id})
    assert r.covered_screens() == [1]


def test_an_unset_or_missing_ghost_display_resolves_to_the_virtual_one():
    """Nothing picked, or the picked display gone (Windows renumbers them):
    the ghost goes to a display that is not the primary - the virtual one
    first - and screen care spares exactly that one."""
    r = Rig(blackout_idle_min=1)
    r.cfg.set("ghost.screen_name", "")
    r.idle = 2 * MIN
    r.care.tick(0.0, Ghost())
    assert r.covered_screens() == [0, 1]
    assert sc.ghost_display(r.cfg, [MAIN, SIDE, GHOST]) is GHOST
    r.cfg.set("ghost.screen_name", r"\\.\DISPLAY9")     # set, but not plugged in
    assert sc.ghost_display(r.cfg, [MAIN, SIDE, GHOST]) is GHOST


def test_without_a_virtual_display_any_non_primary_one_is_used():
    r = Rig(blackout_idle_min=1)
    r.cfg.set("ghost.screen_name", "")
    assert sc.ghost_display(r.cfg, [MAIN, SIDE]) is SIDE
    assert sc.ghost_display(r.cfg, [MAIN]) is None, "the primary alone: nothing better to pick"
    r.cfg.set("ghost.screen_name", MAIN.name)            # picked on purpose: honoured
    assert sc.ghost_display(r.cfg, [MAIN, SIDE, GHOST]) is MAIN


def test_the_ghost_display_can_be_chosen_by_index():
    r = Rig(blackout_idle_min=1, screen_name="", screen_index=0)
    r.idle = 2 * MIN
    r.care.tick(0.0, Ghost())
    assert r.covered_screens() == [1, 2]


def test_a_single_display_has_nothing_to_cover():
    r = Rig(blackout_idle_min=1)
    r.displays = [GHOST]
    r.idle = 2 * MIN
    r.care.tick(0.0, Ghost())
    assert r.launched == []


def test_off_windows_idle_time_is_unknown_so_nothing_is_covered():
    r = Rig(blackout_idle_min=1)
    r.idle = None
    r.care.tick(0.0, Ghost())
    assert r.launched == []


def test_close_takes_every_cover_down():
    r = Rig(blackout_idle_min=1)
    r.idle = 2 * MIN
    r.care.tick(0.0, Ghost())
    r.care.close()
    assert all(p.terminated for p in r.launched) and r.care.covers == {}


def test_cover_is_a_black_topmost_window_that_cannot_take_the_ghosts_pipe():
    cfg = Config({"ghost": {"mpv_path": r"C:\mpv\mpv.exe"}})
    args = cover_args(cfg, 1)
    assert args[0] == r"C:\mpv\mpv.exe"
    for want in ("--ontop", "--fullscreen=yes", "--screen=1", "--fs-screen=1", "--focus-on=never",
                 "--cursor-autohide=always", "--input-default-bindings=no"):
        assert want in args
    assert args[-1].startswith("av://lavfi:color=c=black")
    assert not any(a.startswith("--input-ipc-server") for a in args)


def test_a_cover_can_always_be_clicked_away():
    """The fallback should Hue Ghost ever not be there to remove it: the
    bindings file ships next to the module, and a click or Esc quits."""
    import os
    conf = next(a.split("=", 1)[1] for a in cover_args(Config({}), 0) if a.startswith("--input-conf="))
    assert os.path.isfile(conf)
    binds = dict(line.split(None, 1) for line in open(conf, encoding="utf-8")
                 if line.strip() and not line.startswith("#"))
    assert binds["MBTN_LEFT"].strip() == "quit" and binds["ESC"].strip() == "quit"
    assert "MOUSE_MOVE" not in binds          # a window appearing under the cursor gets one of those


def test_a_cover_that_cannot_launch_is_reported_not_retried(caplog):
    r = Rig(blackout_idle_min=1)

    def broken(args):
        raise OSError("mpv not found")

    r.care._launch = broken
    r.idle = 2 * MIN
    r.care.tick(0.0, Ghost())
    r.care.tick(0.25, Ghost())
    assert r.care.covers == {}
    assert sum("could not black out" in m for m in caplog.messages) == 2      # once per display


# -- the daemon ----------------------------------------------------------------------------
@pytest.fixture
def daemon(tmp_path, monkeypatch):
    from hueghost import daemon as dm
    monkeypatch.setattr(dm, "keep_awake", lambda on, display=True: True)
    monkeypatch.setattr(dm, "desktop_locked", lambda: False)
    cfg = Config({"jellyfin": {"url": "http://jf", "api_key": "k", "follow": {"device_id": "atv"}},
                  "engine": {"type": "none"}, "control": {"port": 0}}, str(tmp_path / "config.json"))
    return dm.Daemon(cfg)


class AliveGhost(Ghost):
    def alive(self):
        return True


def test_daemon_cares_for_a_video_ghost_only(daemon):
    seen = []
    daemon.care.tick = lambda now, ghost, spare=frozenset(), watched=False: seen.append((ghost, set(spare)))
    daemon._screen_care(0.0)
    g = AliveGhost()
    daemon.ghost = g
    daemon._screen_care(1.0)
    daemon._ghost_audio_only = True           # music: no picture to care for
    daemon._screen_care(2.0)
    assert [s[0] for s in seen] == [None, g, None]


def test_daemon_spares_the_display_hue_sync_reports_capturing(daemon):
    seen = []
    daemon.care.tick = lambda now, ghost, spare=frozenset(), watched=False: seen.append(set(spare))
    real_state = daemon.engine.state

    def state():
        st = real_state()
        st.monitor_id = GHOST.monitor_id
        return st

    daemon.engine.state = state
    daemon.ghost = AliveGhost()
    daemon._screen_care(0.0)
    assert seen == [{GHOST.monitor_id}]


def test_daemon_tells_screen_care_when_this_pc_plays(daemon):
    from types import SimpleNamespace
    seen = []
    daemon.care.tick = lambda now, ghost, spare=frozenset(), watched=False: seen.append(watched)
    pc = SimpleNamespace(id="pc", last=[SimpleNamespace(playing=False)])
    daemon.sources.sources.append(pc)
    daemon._screen_care(0.0)
    pc.last = [SimpleNamespace(playing=False), SimpleNamespace(playing=True)]
    daemon._screen_care(1.0)
    assert seen == [False, True]


def test_a_ghost_window_on_the_wrong_display_is_moved_back(daemon, monkeypatch):
    """The Alienware's DisplayPort drops out of Windows while it sleeps, so the
    index the ghost was launched with can point at another screen by the time
    mpv opens its window. Hue Sync then captured an empty desktop."""
    from hueghost import daemon as dm
    from hueghost.ghost import GhostPlayer
    monkeypatch.setattr(dm, "list_displays", lambda: [MAIN, SIDE, GHOST])
    daemon.cfg.set("ghost.screen_name", GHOST.name)
    g = GhostPlayer.__new__(GhostPlayer)
    g.displays, g.checked_on, g.placed_mono, g.placing, g.placed = None, None, float("-inf"), False, []
    g.place = lambda index, now: (g.placed.append(index), setattr(g, "placed_mono", now),
                                  setattr(g, "placing", True))
    g.finish_place = lambda: (g.placed.append("fullscreen"), setattr(g, "placing", False))
    daemon._keep_on_display(g, 0.0)
    assert g.placed == []                       # mpv has not said yet
    g.displays = [MAIN.name]
    daemon._keep_on_display(g, 1.0)
    daemon._keep_on_display(g, 2.0)             # gave it time to get there
    assert g.placed == [GHOST.index, "fullscreen"], "out of fullscreen, then back in on the next tick"
    daemon._keep_on_display(g, 4.5)
    daemon._keep_on_display(g, 4.75)
    assert g.placed == [GHOST.index, "fullscreen"] * 2, "still not there: try again"
    g.displays = [GHOST.name.lower()]
    daemon._keep_on_display(g, 9.0)
    assert len(g.placed) == 4 and g.checked_on == g.displays


def test_screen_care_settings_are_live_and_reported(daemon):
    daemon.action("set", {"pixel_shift_min": 2, "blackout_idle_min": 12.5})
    care = daemon.status()["system"]["screen_care"]
    assert care["pixel_shift_min"] == 2 and care["blackout_idle_min"] == 12.5
    assert care["blacked_out"] == [] and care["pan"] == [0.0, 0.0]
    daemon.action("set", {"blackout_idle_min": -3})
    assert daemon.care.blackout_after_s == 0.0
    with pytest.raises(ValueError):
        daemon.action("set", {"pixel_shift_min": "often"})


def test_defaults_shift_off_blackout_after_a_minute():
    care = ScreenCare(Config({}), idle=lambda: None, displays=list, launch=Proc)
    assert care.shift_every_s == 0.0 and care.blackout_after_s == 1 * MIN


def test_a_bad_value_in_the_file_falls_back_rather_than_crashing_the_loop():
    care = ScreenCare(Config({"ghost": {"pixel_shift_min": "soon", "blackout_idle_min": None}}),
                      idle=lambda: None, displays=list, launch=Proc)
    assert care.shift_every_s == 3 * MIN and care.blackout_after_s == 0.0


def test_a_launched_cover_is_tied_to_this_process(monkeypatch):
    """A black window over someone's monitor must not outlive a crashed Hue Ghost."""
    calls, tied = [], []
    monkeypatch.setattr(sc.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)) or "proc")
    monkeypatch.setattr(sc, "kill_on_exit", tied.append)
    assert sc._launch(["mpv", "x"]) == "proc"
    assert calls[0][0] == ["mpv", "x"] and calls[0][1]["stdin"] == sc.subprocess.DEVNULL
    assert tied == ["proc"]
