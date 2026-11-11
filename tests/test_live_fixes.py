"""What the live test of 2.13.6 turned up (a film on an Apple TV, music on a phone)."""
from hueghost.config import Config
from hueghost.daemon import Daemon


def _daemon(tmp_path, **over):
    data = {"engine": {"type": "none"}, "control": {"port": 0}, "jellyfin": {"api_key": "k"},
            "sources": [{"source": "jellyfin", "device_id": "s23", "name": "Android S23",
                         "area_id": "a1", "area_name": "Music area living + dining"}]}
    data.update(over)
    return Daemon(Config(data, str(tmp_path / "config.json")))


def test_an_area_renamed_in_the_hue_app_shows_its_new_name(tmp_path):
    d = _daemon(tmp_path)
    try:
        d.engine.areas = lambda: [{"id": "a1", "name": "Living music"}, {"id": "a2", "name": "Office"}]
        d.areas()
        assert d.cfg.bindings()[0]["area_name"] == "Living music"
        assert d._binding_status()[0]["area_name"] == "Living music"
        assert Config.load(d.cfg.path).bindings()[0]["area_name"] == "Living music"      # and it is saved
        d.engine.areas = lambda: []                  # Hue Sync's files unreadable: keep what we have
        d._areas_read = -1e9
        d.areas()
        assert d.cfg.bindings()[0]["area_name"] == "Living music"
    finally:
        d.engine.close()


# -- songs began early: the music ghost runs behind its player ----------------------------

from hueghost import daemon as dm  # noqa: E402
from tests.test_daemon_flow import MONO0, RunningGhost, _music, world  # noqa: E402,F401  (fixture)


class Mpv(RunningGhost):
    """Like mpv: a file loaded plays at once, from the position it opens at."""

    def swap(self, url, start=None):
        super().swap(url, start)
        self._time_pos, self._t0 = start or 0.0, self.clock()
        return True


def test_a_ghost_behind_its_player_waits_for_its_start_and_the_old_song_plays_out(world, monkeypatch):  # noqa: F811
    """Live, 11 Nov: music offset -1.85 s, and every song began with the ghost
    1.2-1.5 s AHEAD (mpv played from 0 at once), then seeked for five seconds."""
    d = world.d
    d.cfg.set("ghost.audio_device", "{0.0.0.00000000}.{silent}")
    d.cfg.set("sync.music_offset_s", -1.85)

    def launch(cfg_, url, hdr, start, item_id, err_path=None, **kw):
        g = Mpv(item_id, lambda: MONO0 + world.t, start)
        world.launched.append(g)
        return g
    monkeypatch.setattr(dm.GhostPlayer, "launch", staticmethod(launch))

    def play(item, seconds):
        """The phone plays ``item`` from its start; how far AHEAD the ghost got."""
        t0, lead, items = world.t, [], []
        while world.t < t0 + seconds - 1e-9:
            world.step(0.5, _music(world.t - t0, world.t, item=item))
            g, m = world.ghost, d.last_obs.model
            items.append(g.item_id)
            if g.item_id == item:
                # before the ghost's own start it can only wait at 0
                lead.append(g.pos - max(0.0, m.position_at(MONO0 + world.t) + d.offset))
        return max(lead), items

    ahead, _ = play("track1", 12.0)
    assert ahead < 0.3, ahead                       # was ~1.5
    g = world.ghost
    assert abs(g.pos - (d.last_obs.model.position_at(MONO0 + world.t) - 1.85)) < 0.1 and not g.paused

    ahead, items = play("track2", 12.0)
    assert items[:2] == ["track1", "track1"], items[:6]     # the old song's last 1.85 s still play
    assert items[-1] == "track2" and ahead < 0.3, (ahead, items)
    assert abs(g.pos - (d.last_obs.model.position_at(MONO0 + world.t) - 1.85)) < 0.1 and not g.paused
    assert len(world.launched) == 1                 # hot-swapped, not relaunched


# -- a player that says "paused" seconds late ----------------------------------------------

from tests.test_daemon_flow import playing, running  # noqa: E402,F401  (fixture)


class Tv:
    """A TV that reports every second, and a pause only ``late`` s after it."""

    def __init__(self, world):
        self.w, self.pos = world, 100.0

    def play(self, seconds):
        for _ in range(int(seconds)):
            self.pos += 1.0
            self.w.step(1.0, playing(self.pos, self.w.t))

    def pause(self, late):
        """-> how far past the paused frame the ghost was when the pause arrived."""
        self.pos += 1.0                         # it pauses now, a second after its last report
        if late:
            self.w.step(late)                   # ... and is silent: the session stands as it was
        over = self.w.ghost.pos - self.pos
        self.w.step(3.0, playing(self.pos, self.w.t, paused=True))
        assert self.w.ghost.paused and abs(self.w.ghost.pos - self.pos) < 1.0    # the resume lands exactly
        return over


def test_a_player_that_reports_its_pauses_late_is_learned_and_then_held(running):  # noqa: F811
    """Live, 11 Nov: Moonfin on the Apple TV said "paused" 5.6 s late, and the
    lights ran on that long. Learned per player - nothing here knows Moonfin."""
    d, tv = running.d, Tv(running)
    tv.play(20)
    profile = d.last_obs.model.profile
    assert tv.pause(5.0) > 4.0 and profile.pause_late_s >= 1.0      # not known yet: it runs on, and is learned
    tv.play(20)
    over = tv.pause(5.0)
    assert over < 1.0, over                     # the ghost waited where the reports stopped
    assert profile.pause_late_s >= 1.0          # ... and the lateness is still measured in full

    tv.play(20)
    running.step(1.0)                           # one report a little late is not a pause
    assert not running.ghost.paused
    tv.play(5)
    for _ in range(4):                          # the app is fixed: it says so at once again
        tv.pause(0.0)
        tv.play(10)
    assert profile.pause_late_s < 1.0
    assert tv.pause(5.0) > 4.0                  # so nothing is held on a guess any more


def test_a_player_that_buffers_at_the_start_has_not_paused_late(running):  # noqa: F811
    """Live, 12 Nov: Moonfin said "playing", buffered for 5 s, then reported
    "paused" on the frame it started from. Nothing was paused late."""
    d = running.d
    for _ in range(5):
        running.step(1.0, playing(100.0, running.t))        # says playing, the picture stands
    running.step(1.0, playing(100.0, running.t, paused=True))
    assert d.last_obs.model.profile.pause_late_s == 0.0
