"""Daemon orchestration on a virtual clock: the two-stage stop (lights off
fast, ghost kept on standby, no relaunch when the TV comes straight back),
the pause timeout, and the display power handling.

The Jellyfin side is scripted through the watcher (real position model, fake
sessions), mpv is a FakeGhost, the light engine is the NullEngine (its
``syncing`` mirrors ``desired_sync``), and the Windows power helpers are
recorded instead of called.
"""
import pytest

from hueghost import daemon as dm
from hueghost.config import Config
from hueghost.daemon import IDLE, STANDBY, SYNCING, Daemon
from hueghost.lockstep import Pause, Resume, Seek
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
        self.seeks = 0
        self.nudges = 0
        self.actions = []

    class _Proc:
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
    launched = []

    def launch(cfg_, url, hdr, start, item_id, err_path=None):
        g = FakeGhost(item_id)
        g._time_pos = start
        launched.append(g)
        return g

    monkeypatch.setattr(dm.GhostPlayer, "launch", staticmethod(launch))
    d = Daemon(cfg)
    d.jf.stream_url = lambda item_id, msid=None: "http://jf/stream/" + item_id
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
