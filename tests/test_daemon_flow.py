"""Daemon orchestration on a virtual clock: the two-stage stop (lights off
fast, ghost kept on standby, no relaunch when the TV comes straight back),
the pause timeout, and the display power handling.

The Jellyfin side is scripted through the watcher (real position model, fake
sessions), mpv is a FakeGhost, the light engine is the NullEngine (its
``syncing`` mirrors ``desired_sync``), and the Windows power helpers are
recorded instead of called.
"""
import time

import pytest

from hueghost import daemon as dm
from hueghost.config import Config
from hueghost.daemon import IDLE, STANDBY, SYNCING, Daemon
from hueghost.engines import Plan
from hueghost.lockstep import Pause, Resume, Seek, Speed
from hueghost.sources import Activity, Source
from tests.test_watcher import LOCAL0, MONO0, session

POLL = 0.5


class FakeGhost:
    def __init__(self, item_id):
        self.item_id = item_id
        self._alive = True
        self._time_pos = 100.0
        self.paused = False
        self.speed = 1.0
        self.buffering = False
        self.stalled = False
        self.end_reason = None
        self.user_quit = False
        self.last_seek_mono = float("-inf")
        self.started_mono = float("inf")   # "ran for ever": never a healthy refund by accident
        self.seeks = 0
        self.nudges = 0
        self.actions = []
        self.swapped: list[str] = []

    class _Proc:
        pid = 4242            # the PC probe is told to ignore our own ghost

        def poll(self):
            return 0

    proc = _Proc()

    def alive(self):
        return self._alive

    def kill(self):
        self._alive = False

    @property
    def has_position(self):
        return True

    @property
    def pos(self):
        return self._time_pos

    def apply(self, actions, now):
        for a in actions:
            self.actions.append(a)
            if isinstance(a, Pause):
                self.paused = True
            elif isinstance(a, Resume):
                self.paused = False
            elif isinstance(a, Seek):
                self._time_pos = a.pos
                self.last_seek_mono = now
                self.seeks += 1

    def swap(self, url: str) -> bool:
        self.swapped.append(url)
        self.end_reason = None
        self.eof = False
        self.paused = False
        self._time_pos = 0.0     # the new file's first position
        return True


class Power:
    def __init__(self):
        self.calls = []

    def keep_awake(self, on):
        self.calls.append(("keep", on))
        return True

    def wake(self):
        self.calls.append(("wake",))
        return True


@pytest.fixture
def world(monkeypatch, tmp_path):
    cfg = Config({
        "jellyfin": {"url": "http://jf", "api_key": "k", "follow": {"device_id": "atv-device-id"},
                     "poll_interval_s": POLL},
        "engine": {"type": "none"},
        "control": {"port": 0},
        "sync": {"lights_off_delay_s": 1.5, "idle_stop_delay_s": 10.0, "offset_s": 0.0},
        "ghost": {"keep_awake": "playing"},
    }, str(tmp_path / "config.json"))
    power = Power()
    monkeypatch.setattr(dm, "keep_awake", power.keep_awake)
    monkeypatch.setattr(dm, "wake_display", power.wake)
    monkeypatch.setattr(dm, "desktop_locked", lambda: False)
    monkeypatch.setattr(dm, "audio_endpoint_present", lambda endpoint: True)
    launched = []

    def launch(cfg_, url, hdr, start, item_id, err_path=None, **kw):
        g = FakeGhost(item_id)
        g._time_pos = start
        launched.append(g)
        return g

    monkeypatch.setattr(dm.GhostPlayer, "launch", staticmethod(launch))
    d = Daemon(cfg)
    # **kw: the music path asks for a kind, the video path does not
    d.jf.stream_url = lambda item_id, msid=None, **kw: "http://jf/stream/" + item_id
    d.jf.auth_header_for_mpv = lambda: "Authorization: x"

    class World:
        t = 0.0
        sessions = []

        def step(self, seconds=POLL, sessions=None):
            """Advance the virtual clock in poll-sized steps, feeding the daemon."""
            if sessions is not None:
                self.sessions = sessions
            n = max(1, int(round(seconds / POLL)))
            for _ in range(n):
                self.t += POLL
                now = MONO0 + self.t
                local = LOCAL0 + self.t
                obs = d.watcher.observe(self.sessions, local, local, now)
                d.watcher.poll = lambda client, o=obs: o
                with d._lock:
                    d._poll(now)
                    d._tick(now)
                    d._sync_power()
            return now

        @property
        def ghost(self):
            return d.ghost

        @property
        def engine_on(self):
            return d.engine.state().syncing

    w = World()
    w.d, w.power, w.launched = d, power, launched
    return w


def playing(pos, t, paused=False):
    return [session(pos, paused, LOCAL0 + t)]


def stopped():
    s = session(0, False, LOCAL0)
    s.pop("NowPlayingItem")
    s["PlayState"] = {}
    return [s]


def test_play_launches_ghost_and_starts_lights(world):
    world.step(1.0, playing(100.0, 0.5))
    assert world.ghost is not None and world.engine_on and world.d.state == SYNCING
    assert ("wake",) in world.power.calls and ("keep", True) in world.power.calls


def test_two_stage_stop_lights_first_ghost_later(world):
    world.step(1.0, playing(100.0, 0.5))
    g = world.ghost
    world.step(0.5, stopped())              # t: client gone for 0.5 s -> nothing yet
    assert world.engine_on and world.ghost is g and not g.paused
    world.step(1.5)                         # 2.0 s idle -> lights off, ghost parked
    assert not world.engine_on
    assert world.ghost is g and g.alive() and g.paused
    assert world.d.state == STANDBY
    st = world.d.status()
    assert st["ghost"]["standby"] == "stopped"
    # status() reads the real clock (the harness runs on a virtual one): only its shape is checked
    assert isinstance(st["ghost"]["standby_closes_in_s"], float)
    world.step(8.5)                         # 10.5 s idle -> ghost closed
    assert world.ghost is None and not g.alive() and world.d.state == IDLE
    assert world.power.calls[-1] == ("keep", False)


def test_client_back_within_window_relights_without_relaunch(world):
    world.step(1.0, playing(100.0, 0.5))
    g = world.ghost
    world.step(3.0, stopped())
    assert not world.engine_on and g.paused
    world.step(1.0, playing(104.0, world.t))
    assert world.ghost is g, "the parked ghost must be reused"
    assert len(world.launched) == 1
    assert world.engine_on and world.d.state == SYNCING
    # the position model holds the ghost for the client's learned start-stall
    # (a real restart shows a still frame first), then it resumes on target
    world.step(4.0)
    assert not g.paused and any(isinstance(a, Resume) for a in g.actions)


def test_one_poll_blip_changes_nothing(world):
    world.step(1.0, playing(100.0, 0.5))
    g = world.ghost
    world.step(0.5, stopped())
    assert world.engine_on and world.d._standby is None
    world.step(1.0, playing(101.5, world.t))
    assert world.ghost is g and world.engine_on and world.d._standby is None
    assert len(world.launched) == 1 and world.d.state == SYNCING


def test_new_item_after_stop_relaunches(world):
    world.step(1.0, playing(100.0, 0.5))
    world.step(3.0, stopped())
    world.step(1.0, [session(5.0, False, LOCAL0 + world.t, item="item2")])
    assert world.ghost is not None and world.ghost.item_id == "item2"
    assert len(world.launched) == 2 and world.engine_on


def test_pause_timeout_turns_lights_off_and_play_turns_them_on(world):
    world.d.cfg.set("sync.pause_stop_min", 1.0)
    world.step(1.0, playing(100.0, 0.5))
    g = world.ghost
    world.step(1.0, playing(101.0, world.t, paused=True))
    assert world.engine_on and g.paused                  # short pause: lights stay on
    world.step(61.0)
    assert not world.engine_on and world.d.state == STANDBY
    assert world.d.status()["ghost"]["standby"] == "paused"
    world.step(1.0, playing(101.0, world.t))
    assert world.engine_on and world.ghost is g and world.d.state == SYNCING


def test_disabled_sync_stops_at_once(world):
    world.step(1.0, playing(100.0, 0.5))
    world.d.set_enabled(False)
    assert world.ghost is None and not world.engine_on
    world.step(0.5)
    assert world.power.calls[-1] == ("keep", False)


def test_keep_awake_modes(world):
    world.d.cfg.set("ghost.keep_awake", "off")
    world.step(1.0, playing(100.0, 0.5))
    assert not any(c[0] in ("keep", "wake") for c in world.power.calls)
    world.d.set_enabled(False)
    world.step(0.5)
    world.d.cfg.set("ghost.keep_awake", "always")
    world.step(0.5)
    assert world.power.calls[-1] == ("keep", True)       # held without a ghost
    world.d.cfg.set("ghost.keep_awake", "playing")
    world.step(0.5)
    assert world.power.calls[-1] == ("keep", False)


def test_settings_action_validates_keep_awake(world):
    world.d.action("set", {"keep_awake": "always", "lights_off_delay_s": 2.5})
    assert world.d.keep_awake_mode() == "always" and world.d.lights_off_delay == 2.5
    with pytest.raises(ValueError):
        world.d.action("set", {"keep_awake": "sometimes"})


def _music(pos, t, paused=False, item="track1", runtime=240.0):
    """One music session, as a phone app reports it."""
    s = session(pos, paused, LOCAL0 + t, item=item)
    s["NowPlayingItem"] = {"Id": item, "Name": "Song", "MediaType": "Audio",
                           "Type": "Audio", "Album": "Album",
                           "RunTimeTicks": int(runtime * 10_000_000)}
    return [s]


def test_music_paused_in_the_background_stops_instead_of_waiting_on_standby(world):
    """A phone that stops a track usually leaves the session open rather than
    closing it, so music cannot wait for the minutes a film is given - and it
    is not parked on standby either: nothing is coming back to a paused song,
    so the ghost is closed and the source stops looking busy."""
    world.d.cfg.set("sync.pause_stop_min", 0.0)        # films: never, the default
    # music only syncs into an output nobody can hear
    world.d.cfg.set("ghost.audio_device", "{0.0.0.00000000}.{silent}")
    world.step(1.0, _music(30.0, 0.5))
    assert world.engine_on
    g = world.ghost
    world.step(2.0, _music(31.0, world.t, paused=True))
    assert world.engine_on                             # a short gap is not the end
    world.step(15.0)
    assert not world.engine_on and world.d.state == IDLE
    assert world.ghost is None and not g.alive()
    assert world.d.status()["ghost"]["standby"] is None
    # ... and it stays off while the session sits there paused
    world.step(30.0, _music(31.0, world.t, paused=True))
    assert world.ghost is None and not world.engine_on
    # the next track brings it straight back, with a fresh ghost
    world.step(1.0, _music(31.0, world.t))
    assert world.engine_on and world.d.state == SYNCING
    assert world.ghost is not None and len(world.launched) == 2


def test_music_skip_hot_swaps_without_stopping_the_lights(world):
    """Skipping a song on a music source must not blink the lights: the ghost
    swaps files in place and the engine never stops."""
    world.d.cfg.set("ghost.audio_device", "{0.0.0.00000000}.{silent}")
    world.step(1.0, _music(30.0, 0.5))
    g = world.ghost
    assert world.engine_on and len(world.launched) == 1
    world.step(1.0, _music(1.0, world.t, item="track2"))
    assert world.ghost is g, "the track change must keep the ghost process"
    assert g.item_id == "track2" and g.swapped == ["http://jf/stream/track2"]
    assert len(world.launched) == 1, "a swap must not consume a launch"
    assert world.engine_on and world.d.state == SYNCING
    world.step(2.0)
    assert any(isinstance(a, Seek) for a in g.actions), "it lands on the new anchor"


def test_music_eof_between_tracks_keeps_the_engine(world):
    """The ghost reaches the end of its file a moment before the client reports
    the next track: that gap used to stop and restart the sync."""
    world.d.cfg.set("ghost.audio_device", "{0.0.0.00000000}.{silent}")
    world.step(1.0, _music(30.0, 0.5, item="track1"))
    g = world.ghost
    g.end_reason = "eof"
    g._alive = False
    world.step(1.0, _music(239.0, world.t, item="track1"))
    assert world.engine_on, "the engine rides through the eof gap"
    assert world.ghost is None and world.d._eof_item == "track1"
    assert len(world.launched) == 1, "the finished track is not relaunched"
    world.step(1.0, _music(1.0, world.t, item="track2"))
    assert world.ghost is not None and world.ghost.item_id == "track2"
    assert len(world.launched) == 2 and world.engine_on


def test_finished_track_does_not_launch_a_ghost(world):
    """A phone that finished a song sits at its last position; launching a
    ghost there sent mpv past the end of the file, where it died rc=2 in a
    loop and burned the launch budget (the 10 s sync starts)."""
    world.d.cfg.set("ghost.audio_device", "{0.0.0.00000000}.{silent}")
    world.step(1.0, _music(239.9, 0.5, item="track1"))
    assert world.ghost is None and len(world.launched) == 0
    world.step(2.0, _music(239.9, world.t, item="track1"))
    assert world.ghost is None and len(world.launched) == 0, "still waiting: the track is over"
    world.step(1.0, _music(238.0, world.t, item="track2"))
    assert len(world.launched) == 1 and world.ghost.item_id == "track2"
    assert world.launched[0]._time_pos <= 239.0, "the start never lands past the end of the file"


def test_healthy_run_refunds_the_launch_budget(world):
    """A ghost that ran for real (>=30 s) proves the environment works: its
    launch must stop counting against the crash-loop window, or one rough
    patch of rc=2 deaths blocks the next track for minutes."""
    world.d.cfg.set("ghost.audio_device", "{0.0.0.00000000}.{silent}")
    world.step(1.0, _music(30.0, 0.5, item="track1"))
    g = world.ghost
    world.d._launches.extend([MONO0 + world.t] * 5)    # a storm's worth of launches
    # _stop_ghost measures the run against the REAL clock (it has no tick);
    # a fresh CI runner's time.monotonic() can be seconds past boot, so the
    # fake ghost's age must be set on the same real clock.
    g.started_mono = time.monotonic() - 60             # but this one ran 60 s
    g.end_reason = "eof"
    g._alive = False
    world.step(1.0, _music(1.0, world.t, item="track2"))
    assert world.ghost is not None and len(world.launched) == 2, "the refund happened"


def test_missing_audio_output_blocks_launch_and_wakes_the_display(world, monkeypatch):
    """The endpoint the ghost's music plays into rides on a display; asleep, it
    vanishes. No launch then (mpv would die rc=2) - wake the display instead
    and retry on a later poll."""
    monkeypatch.setattr(dm, "audio_endpoint_present", lambda endpoint: False)
    world.d.cfg.set("ghost.audio_device", "{0.0.0.00000000}.{silent}")
    world.step(1.0, _music(30.0, 0.5))
    assert world.ghost is None and len(world.launched) == 0
    assert ("wake",) in world.power.calls
    world.step(2.0, _music(31.0, world.t))
    assert world.ghost is None, "still no launch while the endpoint is gone"
    monkeypatch.setattr(dm, "audio_endpoint_present", lambda endpoint: True)
    world.step(1.0, _music(32.0, world.t))
    assert world.ghost is not None and world.engine_on


def test_video_item_change_still_relaunches(world):
    """Only music hot-swaps: a video ghost swap would show the wrong picture on
    the captured display, so a new item keeps the stop/relaunch dance."""
    world.step(1.0, playing(100.0, 0.5))
    g = world.ghost
    world.step(1.0, [session(5.0, False, LOCAL0 + world.t, item="item2")])
    assert world.ghost is not g and world.ghost.item_id == "item2"
    assert len(world.launched) == 2 and world.engine_on


class FakePcSource(Source):
    """An app on this PC: it plays, it needs no ghost, and it wants its own
    mode, intensity and entertainment area."""
    id = "pc"

    def __init__(self, plan, bid="spotify", rank=-1):
        super().__init__()
        self.interval_s = 0.5
        self._act = Activity(
            binding={"id": bid, "rank": rank, "name": bid, "area_name": "Office"},
            seen=True, playing=True, kind="music", needs_ghost=False,
            plan=plan, title=bid, event="new_item")

    def poll(self, now):
        return self._act

    def ignore_pid(self, pid):
        self.ignored = pid


def test_an_app_on_this_pc_taking_over_parks_the_ghost_and_moves_the_lights(world):
    """The binding that drives the lights can change without either source
    seeing anything new. The losing client's mpv used to keep running for as
    long as the app played (only ``_idle`` ever closed one, and it does not run
    while something else plays), and Hue Sync kept the old mode and area."""
    world.step(1.0, playing(100.0, 0.5))
    g = world.ghost
    assert g is not None and world.d._plan_applied.mode == "video"
    plan = Plan(area_id="office", mode="music", intensity="subtle")
    world.d.sources.hold_s = 0.0             # arbitration is tested in test_sources
    world.d.sources.sources.append(FakePcSource(plan))
    world.step(1.0)
    assert world.d._plan_applied == plan, "the winner's mode/intensity/area must be applied"
    assert world.engine_on and world.d.state == SYNCING
    # the ghost holds its frame first: a source that has the lights for a
    # moment must not cost a relaunch
    assert world.ghost is g and g.paused and world.d.status()["ghost"]["parked"]
    world.step(10.0)
    assert world.ghost is None and not g.alive(), "the losing client's mpv must be closed"


def test_a_parked_ghost_comes_back_without_a_relaunch(world):
    """... and the client that lost the lights for a few seconds picks its own
    ghost back up, exactly as one that briefly stopped does."""
    world.step(1.0, playing(100.0, 0.5))
    g = world.ghost
    world.d.sources.hold_s = 0.0
    pc = FakePcSource(Plan(area_id="office", mode="music", intensity="subtle"))
    world.d.sources.sources.append(pc)
    world.step(3.0)
    assert world.ghost is g and g.paused
    pc._act = Activity(binding=pc._act.binding, seen=True, playing=False,
                       needs_ghost=False, plan=pc._act.plan)
    world.step(1.0, playing(106.0, world.t))
    assert world.ghost is g and len(world.launched) == 1, "no relaunch"
    assert world.d._plan_applied.mode == "video"
    world.step(4.0)                          # ... and it re-anchors and plays on
    assert not g.paused and any(isinstance(a, Resume) for a in g.actions)


def test_the_client_coming_back_takes_its_own_plan_with_it(world):
    """... and back again: the plan is re-applied on every change of winner,
    not only when a source reports a new item."""
    world.d.sources.hold_s = 0.0
    pc = FakePcSource(Plan(area_id="office", mode="music", intensity="subtle"))
    world.d.sources.sources.append(pc)
    world.step(1.0, playing(100.0, 0.5))
    assert world.d._plan_applied.mode == "music" and world.ghost is None
    pc._act = Activity(binding=pc._act.binding, seen=True, playing=False,
                       needs_ghost=False, plan=pc._act.plan)
    world.step(1.0, playing(102.0, world.t))
    assert world.ghost is not None and world.d._plan_applied.mode == "video"


def test_home_reports_the_mode_and_intensity_that_are_live(world):
    """Every source carries its own mode and intensity now, so the global
    settings are only a fallback. Reporting them left every control that shows
    "the mode" - Home, the tray, the CLI, Home Assistant - on whatever the
    previous session happened to leave behind, all through this one."""
    from hueghost.engines import EngineState

    d = world.d
    d.cfg.set("engine.huesync.mode", "music")          # left over from last time
    d.cfg.set("engine.huesync.intensity", "high")
    d.engine.state = lambda: EngineState(name="x", connected=True, syncing=True,
                                         mode="video", intensity="subtle")
    st = d.status()
    assert (st["mode"], st["intensity"]) == ("video", "subtle")
    assert (st["mode_default"], st["intensity_default"]) == ("music", "high")
    assert (st["engine"]["mode"], st["engine"]["intensity"]) == ("video", "subtle")


def test_a_mode_hue_ghost_has_no_control_for_falls_back_to_the_default(world):
    """Hue Sync also has "scenes", which syncs nothing and has no button here -
    and before it connects there is nothing live to report at all."""
    from hueghost.engines import EngineState

    d = world.d
    d.cfg.set("engine.huesync.mode", "video")
    d.cfg.set("engine.huesync.intensity", "high")
    d.engine.state = lambda: EngineState(name="x", connected=True, mode="scenes")
    assert (d.status()["mode"], d.status()["intensity"]) == ("video", "high")
    d.engine.state = lambda: EngineState(name="x", connected=False)
    assert (d.status()["mode"], d.status()["intensity"]) == ("video", "high")


def test_the_controls_follow_the_hue_sync_app_across_a_restart(world):
    """Hue Ghost restarts the app itself to apply an area, and the user can
    restart it too. The engine keeps the last mode and intensity the app
    reported while the socket is down, so the controls hold what the app said
    instead of flicking back to a saved default mid-restart - and they pick up
    whatever it says the moment it is back."""
    from hueghost.engines import EngineState

    d = world.d
    d.cfg.set("engine.huesync.mode", "music")        # a stale default, nobody chose it
    d.cfg.set("engine.huesync.intensity", "high")
    live = {"mode": "video", "intensity": "subtle", "connected": True, "state": "syncing"}
    d.engine.state = lambda: EngineState(name="x", **live)
    assert (d.status()["mode"], d.status()["intensity"]) == ("video", "subtle")
    live.update(connected=False, state=None)         # the app is killed to apply an area
    assert (d.status()["mode"], d.status()["intensity"]) == ("video", "subtle")
    live.update(connected=True, state="syncing", mode="games", intensity="extreme")
    assert (d.status()["mode"], d.status()["intensity"]) == ("games", "extreme")


def test_a_mode_picked_in_the_app_is_not_saved_over_a_source_that_sets_its_own(world):
    """The saved setting is the default for sources that have *not* chosen a
    mode of their own, so a mode picked during one film must not quietly
    become every other source's. The engine keeps the user's choice for the
    session either way; only where it is stored changes."""
    d = world.d
    d.cfg.set("engine.huesync.mode", "video")
    world.step(1.0, playing(100.0, 0.5))
    assert d.last_act.playing and d.last_act.binding is not None

    d.last_act.binding["mode"] = "video"             # this source sets its own
    d._engine_events.put({"mode": "music"})
    d._drain_engine_events()
    assert d.cfg.get("engine.huesync.mode") == "video", "the default must not drift"

    d.last_act.binding["mode"] = ""                  # this one leaves it to the default
    d._engine_events.put({"mode": "music"})
    d._drain_engine_events()
    assert d.cfg.get("engine.huesync.mode") == "music"


def test_a_paused_film_is_not_held_to_the_music_limit(world):
    """The music limit must not leak into video: a film paused for 20 s is
    still a film someone is coming back to."""
    world.step(1.0, playing(100.0, 0.5))
    world.step(1.0, playing(101.0, world.t, paused=True))
    world.step(20.0)
    assert world.engine_on and world.d.state != STANDBY


def test_a_new_ghost_does_not_wake_displays_screen_care_has_blacked_out(world):
    """The wake-up jiggle is input: at every new episode of a binge it would
    take the black away. Blacked-out displays are on anyway - held awake."""
    world.d.care.covers = {r"\\.\DISPLAY1": object()}
    world.step(1.0, playing(100.0, 0.5))
    assert world.ghost is not None and world.engine_on
    assert ("wake",) not in world.power.calls
    world.d.care.covers = {}


# -- the time lock (experimental) ---------------------------------------------------

class RunningGhost(FakeGhost):
    """A ghost whose position moves with the virtual clock at its speed, so
    the lockstep controller has something real to converge."""

    def __init__(self, item_id, clock, start):
        super().__init__(item_id)
        self.clock = clock
        self._time_pos = start
        self._t0 = clock()

    @property
    def pos(self):
        if self.paused:
            return self._time_pos
        return self._time_pos + (self.clock() - self._t0) * self.speed

    def apply(self, actions, now):
        for a in actions:
            self.actions.append(a)
            here = self.pos
            self._time_pos, self._t0 = here, self.clock()
            if isinstance(a, Pause):
                self.paused = True
            elif isinstance(a, Resume):
                self.paused = False
            elif isinstance(a, Speed):
                self.speed = a.value
            elif isinstance(a, Seek):
                self._time_pos = a.pos
                self.last_seek_mono = now
                self.seeks += 1


@pytest.fixture
def running(world, monkeypatch):
    def launch(cfg_, url, hdr, start, item_id, err_path=None, **kw):
        # a little off target, as a real launch is
        g = RunningGhost(item_id, lambda: MONO0 + world.t, start + 0.3)
        world.launched.append(g)
        return g
    monkeypatch.setattr(dm.GhostPlayer, "launch", staticmethod(launch))
    return world


def _tv(world, pos0, seconds, t0=None):
    """The TV plays on from ``pos0`` (at ``t0``) and reports every second."""
    t0 = world.t if t0 is None else t0
    end = world.t + seconds
    while world.t < end - 1e-9:
        world.step(1.0, playing(pos0 + (world.t - t0), world.t))
    return t0


def _drift(world):
    """Ghost vs target on the virtual clock (status() reads the real one)."""
    m = world.d.last_obs.model
    return world.ghost.pos - m.position_at(MONO0 + world.t)


def _time_lock(d, on=True):
    d.apply_config({"sync": {"time_lock": on}})


def test_time_lock_is_off_by_default_and_changes_nothing(running):
    d = running.d
    assert d.params.time_lock is False and d.watcher.precise_timing is False
    _tv(running, 100.0, 60.0)
    st = d.status()
    assert st["time_lock"] == {"enabled": False, "engaged": False, "residual_s": None}
    assert not d.last_obs.model.locked


def test_time_lock_engages_at_zero_drift_and_stops_nudging(running):
    d = running.d
    _time_lock(d)
    assert d.watcher.precise_timing
    t0 = _tv(running, 100.0, 60.0)
    st = d.status()
    assert st["time_lock"]["engaged"], st
    assert abs(_drift(running)) <= 0.05
    g = running.ghost
    n = len(g.actions)
    _tv(running, 100.0, 60.0, t0)
    assert d.status()["time_lock"]["engaged"]
    assert [a for a in g.actions[n:] if not (isinstance(a, Speed) and a.value == 1.0)] == [],         "locked: no nudging, no seeks"
    assert abs(_drift(running)) <= 0.05


def test_a_seek_releases_the_time_lock_and_it_locks_again(running):
    d = running.d
    _time_lock(d)
    _tv(running, 100.0, 60.0)
    assert d.last_obs.model.locked
    g = running.ghost
    seeks = g.seeks
    t0 = _tv(running, 900.0, 2.0)                 # the viewer jumps ahead
    assert not d.last_obs.model.locked
    assert g.seeks > seeks and abs(g.pos - 900.0) < 5.0
    _tv(running, 900.0, 60.0, t0)
    assert d.last_obs.model.locked, "settled again after the seek"


def test_switching_the_time_lock_off_releases_it_at_once(running):
    d = running.d
    _time_lock(d)
    t0 = _tv(running, 100.0, 60.0)
    assert d.last_obs.model.locked
    _time_lock(d, False)
    _tv(running, 100.0, 1.0, t0)
    assert not d.last_obs.model.locked and not d.status()["time_lock"]["enabled"]
    assert not d.watcher.precise_timing


# -- saving a setting must not undo the playing source's own look -------------------

def _record_engine(d):
    calls = []
    d.engine.set_mode = lambda m: calls.append(("mode", m))
    d.engine.set_intensity = lambda i: calls.append(("intensity", i))
    return calls


def test_saving_a_setting_while_playing_leaves_the_sources_mode_alone(world):
    """Any saved setting - an offset nudge, the sync page's Save - used to push
    the global mode and intensity into the engine, flipping a phone playing
    music (music/high) to the global video/subtle mid-song."""
    d = world.d
    d.cfg.set("engine.huesync.mode", "video")
    d.cfg.set("engine.huesync.intensity", "subtle")
    world.step(1.0, playing(100.0, 0.5))
    assert d.last_act.playing
    calls = _record_engine(d)
    d.action("set", {"offset_delta": 0.25})
    d.apply_config({"sync": {"deadband_s": 0.04}})
    assert calls == [], calls


def test_mode_from_home_is_live_but_not_saved_over_a_source_that_sets_its_own(world):
    d = world.d
    d.cfg.set("engine.huesync.mode", "video")
    world.step(1.0, playing(100.0, 0.5))
    calls = _record_engine(d)
    d.last_act.binding["mode"] = "video"             # this source sets its own
    d.action("set", {"mode": "games"})
    assert calls == [("mode", "games")], "the lights change at once"
    assert d.cfg.get("engine.huesync.mode") == "video", "the default must not drift"
    d.last_act.binding["mode"] = ""                  # this one leaves it to the default
    d.action("set", {"mode": "music"})
    assert calls[-1] == ("mode", "music") and d.cfg.get("engine.huesync.mode") == "music"


def test_a_changed_release_applies_to_a_lock_already_held(running):
    d = running.d
    _time_lock(d)
    t0 = _tv(running, 100.0, 60.0)
    m = d.last_obs.model
    assert m.locked and m.lock_release_s == 0.02
    d.apply_config({"sync": {"time_lock_release_s": 0.1}})
    _tv(running, 100.0, 1.0, t0)
    assert m.locked and m.lock_release_s == 0.1


def test_a_client_found_paused_does_not_light_up_until_it_plays(world):
    """2.9.0 at start-up, with a phone sitting on a paused song: the ghost
    launched, the sync started, and 15 s later the pause timeout switched it
    all off again. Nothing is playing, so nothing lights up."""
    world.step(1.0, playing(100.0, 0.5, paused=True))
    assert world.ghost is None and not world.engine_on
    world.step(5.0)
    assert world.ghost is None and not world.engine_on
    world.step(1.0, playing(100.0, world.t))
    assert world.ghost is not None and world.engine_on
