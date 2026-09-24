"""Several sources, one Hue Sync app: who gets the lights.

The app has one entertainment area, one capture display and one mode, so only
one binding can be live. The rule is the user's list order.
"""
import pytest

from hueghost.config import Config
from hueghost.daemon import Daemon
from hueghost.engines import AUTO
from hueghost.pcwatch import AudioHit, Foreground
from hueghost.sources import Activity, Source, SourceSet, build_sources

MON_REAL = r"MONITOR\DELA212\{g}\0001"


class Fake(Source):
    """A source that says whatever the test tells it to."""

    def __init__(self, sid, acts, interval=0.5):
        super().__init__()
        self.id = sid
        self.interval_s = interval
        self._acts = acts
        self.polls = 0

    def poll(self, now):
        self.polls += 1
        return self._acts


def act(bid, rank, playing=True, seen=True):
    return Activity(binding={"id": bid, "rank": rank}, seen=seen, playing=playing,
                    title=bid)


def test_list_order_decides_not_source_order():
    # the PC source is second, but its binding is first in the user's list
    tv = Fake("jellyfin", [act("atv", 1)])
    pc = Fake("pc", [act("game", 0)])
    s = SourceSet([tv, pc], hold_s=0.0)
    assert s.poll(0.0).binding["id"] == "game"


def test_nothing_playing_still_reports_what_is_there():
    tv = Fake("jellyfin", [act("atv", 0, playing=False, seen=True)])
    s = SourceSet([tv], hold_s=0.0)
    got = s.poll(0.0)
    assert got.seen and not got.playing and got.binding["id"] == "atv"


def test_nothing_at_all_is_idle():
    s = SourceSet([Fake("pc", [act("game", 0, playing=False, seen=False)])], hold_s=0.0)
    assert s.poll(0.0).binding is None


def test_the_winner_is_held_briefly_so_the_app_is_not_restarted_every_few_seconds():
    tv = Fake("jellyfin", [act("atv", 1)])
    pc = Fake("pc", [act("game", 0)])
    s = SourceSet([tv], hold_s=20.0)
    assert s.poll(0.0).binding["id"] == "atv"          # only the TV is playing
    s.sources.append(pc)
    # the game starts and outranks it, but switching costs a Hue Sync restart
    assert s.poll(1.0).binding["id"] == "atv"
    assert s.poll(10.0).binding["id"] == "atv"
    assert s.poll(30.0).binding["id"] == "game"        # after the hold, it wins


def test_each_source_polls_at_its_own_rate():
    tv = Fake("jellyfin", [act("atv", 0)], interval=0.5)
    pc = Fake("pc", [act("game", 1)], interval=2.0)
    s = SourceSet([tv, pc], hold_s=0.0)
    for i in range(9):
        s.poll(i * 0.5)                # 0.0 .. 4.0
    assert tv.polls == 9 and pc.polls == 3      # the PC is not polled at the TV's rate


def test_a_source_that_raises_does_not_stop_the_others():
    class Broken(Source):
        id = "pc"

        def poll(self, now):
            raise RuntimeError("COM went away")

    tv = Fake("jellyfin", [act("atv", 1)])
    s = SourceSet([Broken(), tv], hold_s=0.0)
    assert s.poll(0.0).binding["id"] == "atv"
    assert s.sources[0].ok is False and "COM" in s.sources[0].error


# -- what each kind of binding asks the engine for --------------------------
def cfg_with(bindings, **extra):
    data = {"jellyfin": {"url": "http://jf", "api_key": "k"}, "engine": {"type": "none"},
            "control": {"port": 0}, "sources": bindings}
    data.update(extra)
    return Config(data)


def test_a_pc_video_binding_wants_the_real_screen_and_no_ghost():
    cfg = cfg_with([{"source": "pc", "exe": "firefox.exe", "mode": "video",
                     "area_id": "a1", "monitor": MON_REAL}])
    pc = build_sources(cfg).sources[0]
    hits = pc.probe.observe(pc.bindings(), [AudioHit(1, "firefox.exe", 0.5)], None, 0.0)
    hits = pc.probe.observe(pc.bindings(), [AudioHit(1, "firefox.exe", 0.5)], None, 2.0)
    a = pc.activities(hits)[0]
    assert a.playing and a.needs_ghost is False and a.ghost is None and a.obs is None
    assert a.plan.mode == "video" and a.plan.monitor == MON_REAL and a.plan.area_id == "a1"


def test_a_pc_game_binding_asks_for_games_mode_on_the_screen_it_is_on():
    cfg = cfg_with([{"source": "pc", "exe": "eldenring.exe", "mode": "games", "area_id": "a2"}])
    pc = build_sources(cfg).sources[0]
    fg = Foreground(pid=2, exe="eldenring.exe", fullscreen=True, monitor_id=MON_REAL)
    pc.probe.observe(pc.bindings(), [], fg, 0.0)
    a = pc.activities(pc.probe.observe(pc.bindings(), [], fg, 2.0))[0]
    assert a.playing and a.kind == "game"
    assert a.plan.mode == "games" and a.plan.monitor == MON_REAL


def test_pc_music_hands_the_audio_input_back_to_the_app():
    # the sound is already coming out of this PC: Hue Sync should listen to its
    # own default output, undoing any endpoint a Jellyfin-music session pinned
    cfg = cfg_with([{"source": "pc", "exe": "spotify.exe", "mode": "music", "area_id": "a3"}])
    pc = build_sources(cfg).sources[0]
    pc.probe.observe(pc.bindings(), [AudioHit(3, "spotify.exe", 0.4)], None, 0.0)
    a = pc.activities(pc.probe.observe(pc.bindings(), [AudioHit(3, "spotify.exe", 0.4)], None, 2.0))[0]
    assert a.plan.mode == "music" and a.plan.audio_device == AUTO
    assert a.plan.monitor is None          # music captures no screen


def test_a_binding_carries_its_own_mode_and_intensity():
    """Each source is set up once and then simply played: the Apple TV in
    video/subtle, a phone running a music app in music/high."""
    cfg = cfg_with([{"source": "jellyfin", "device_id": "atv", "mode": "video",
                     "intensity": "subtle", "area_id": "living"}],
                   engine={"type": "none", "huesync": {"mode": "games", "intensity": "extreme"}})
    jf = build_sources(cfg).sources[0]
    p = jf._plan(jf.bindings()[0], "video")
    assert p.mode == "video" and p.intensity == "subtle" and p.area_id == "living"


def test_a_binding_with_no_settings_of_its_own_falls_back_to_the_global_ones():
    cfg = cfg_with([{"source": "jellyfin", "device_id": "atv"}],
                   engine={"type": "none", "huesync": {"mode": "games", "intensity": "moderate"}})
    jf = build_sources(cfg).sources[0]
    p = jf._plan(jf.bindings()[0], "video")
    assert p.mode == "games" and p.intensity == "moderate"


def test_music_is_music_mode_whatever_the_binding_asks_for():
    # a song has no picture: the binding's intensity still applies, its mode cannot
    cfg = cfg_with([{"source": "jellyfin", "device_id": "s23", "client": "CAMusic",
                     "mode": "video", "intensity": "high", "kinds": ["music"]}],
                   ghost={"audio_device": "{0.0.0.00000000}.{cable}"})
    jf = build_sources(cfg).sources[0]
    p = jf._plan(jf.bindings()[0], "music")
    assert p.mode == "music" and p.intensity == "high" and p.monitor is None


def test_a_pc_binding_carries_its_own_intensity_too():
    cfg = cfg_with([{"source": "pc", "exe": "spotify.exe", "mode": "music", "intensity": "subtle"}],
                   engine={"type": "none", "huesync": {"intensity": "extreme"}})
    pc = build_sources(cfg).sources[0]
    pc.probe.observe(pc.bindings(), [AudioHit(3, "spotify.exe", 0.4)], None, 0.0)
    a = pc.activities(pc.probe.observe(pc.bindings(), [AudioHit(3, "spotify.exe", 0.4)], None, 2.0))[0]
    assert a.plan.mode == "music" and a.plan.intensity == "subtle"


def test_a_disabled_binding_is_not_polled_at_all():
    cfg = cfg_with([{"source": "pc", "exe": "firefox.exe", "mode": "video", "enabled": False},
                    {"source": "pc", "exe": "vlc.exe", "mode": "video"}])
    pc = build_sources(cfg).sources[0]
    assert [b["exe"] for b in pc.bindings()] == ["vlc.exe"]


def test_a_music_only_binding_with_nowhere_silent_to_play_is_refused():
    cfg = cfg_with([{"source": "jellyfin", "device_id": "atv", "kinds": ["music"]}])
    assert build_sources(cfg).sources == []          # skipped, with a logged reason
    cfg.set("ghost.audio_device", "{0.0.0.00000000}.{cable}")
    assert len(build_sources(cfg).sources) == 1
