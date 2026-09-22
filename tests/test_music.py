"""Jellyfin music.

The watcher used to drop every session that was not Video, so an album playing
on the TV never reached Now Playing and never lit anything. It is followed like
a film now - the position model does not care what the media is - but the ghost
has to do the opposite of its usual job: no picture, real sound, into an output
nobody can hear, which is the one Hue Sync's music mode listens to.
"""
import pytest

from hueghost.config import Config
from hueghost.engines import AUTO
from hueghost.ghost import mpv_args
from hueghost.jellyfin import TICKS_PER_S, JellyfinClient
from hueghost.sources import build_sources
from hueghost.watcher import PlayerSet, SessionWatcher, report_from_session
from tests.test_watcher import DEV, LOCAL0, MONO0, session

CABLE = "{0.0.0.00000000}.{f06b61ae-8af6-4b4e-94f9-f59d03355224}"


def music_session(pos=30.0, t=5.0, item="track1"):
    s = session(pos, False, LOCAL0 + t, item=item)
    s["NowPlayingItem"] = {"Id": item, "Name": "Song", "MediaType": "Audio", "Type": "Audio",
                           "Album": "Album", "RunTimeTicks": int(240 * TICKS_PER_S)}
    return s


def test_an_audio_session_is_a_report_now():
    r = report_from_session(music_session())
    assert r is not None and r.media_type == "music" and r.pos == pytest.approx(30.0)
    # a video session still says video
    assert report_from_session(session(10.0, False, LOCAL0)).media_type == "video"


def test_something_with_no_position_is_still_ignored():
    s = music_session()
    s["NowPlayingItem"]["MediaType"] = "Photo"
    assert report_from_session(s) is None


def test_a_player_followed_for_films_only_ignores_an_album():
    films = PlayerSet.from_players([{"device_id": DEV, "kinds": ["video"]}])
    both = PlayerSet.from_players([{"device_id": DEV, "kinds": ["video", "music"]}])
    sessions = [music_session()]
    s, who = films.pick(sessions)
    assert who is not None and s is not None       # seen...
    w = SessionWatcher(films)
    assert w.observe(sessions, LOCAL0 + 5, LOCAL0 + 5, MONO0 + 5).playing is False   # ...not followed
    w = SessionWatcher(both)
    obs = w.observe(sessions, LOCAL0 + 5, LOCAL0 + 5, MONO0 + 5)
    assert obs.playing and obs.model.media_type == "music"


def cfg_for_music(**extra):
    data = {"jellyfin": {"url": "http://jf", "api_key": "k",
                         "follow": {"device_id": DEV}},
            "engine": {"type": "none"}, "control": {"port": 0}}
    data.update(extra)
    return Config(data)


def test_music_asks_for_music_mode_and_pins_the_output_it_plays_into():
    cfg = cfg_for_music(ghost={"audio_device": CABLE})
    src = build_sources(cfg).sources[0]
    w = SessionWatcher(PlayerSet.from_players(src.bindings()))
    obs = w.observe([music_session()], LOCAL0 + 5, LOCAL0 + 5, MONO0 + 5)
    a = src.activity(obs)
    assert a.playing and a.kind == "music" and a.needs_ghost
    assert a.plan.mode == "music" and a.plan.audio_device == CABLE
    assert a.plan.monitor is None              # music mode captures no screen
    assert a.ghost.audio_only and a.ghost.audio_device == CABLE
    assert "/Audio/track1/stream" in a.ghost.url


def test_without_a_silent_output_music_is_shown_but_not_synced():
    # playing it anywhere else means playing it out loud, so it is not played
    cfg = cfg_for_music()
    src = build_sources(cfg).sources[0]
    w = SessionWatcher(PlayerSet.from_players(src.bindings()))
    obs = w.observe([music_session()], LOCAL0 + 5, LOCAL0 + 5, MONO0 + 5)
    a = src.activity(obs)
    assert a.seen and a.playing is False
    assert a.title == "Song"                   # still shown in Now Playing


def test_a_video_on_the_same_player_is_unaffected():
    cfg = cfg_for_music()
    src = build_sources(cfg).sources[0]
    w = SessionWatcher(PlayerSet.from_players(src.bindings()))
    obs = w.observe([session(10.0, False, LOCAL0 + 5)], LOCAL0 + 5, LOCAL0 + 5, MONO0 + 5)
    a = src.activity(obs)
    assert a.playing and a.kind == "video" and a.ghost.audio_only is False
    assert "/Videos/item1/stream" in a.ghost.url


def test_the_music_ghost_is_audible_windowless_and_on_the_chosen_device():
    cfg = cfg_for_music(ghost={"audio_device": CABLE, "music_volume": 90})
    args = mpv_args(cfg, "t", audio_only=True, audio_device=CABLE)
    assert "--mute=no" in args and "--volume=90" in args
    assert "--vid=no" in args and "--force-window=no" in args
    assert "--audio-device=wasapi/{f06b61ae-8af6-4b4e-94f9-f59d03355224}" in args
    assert not any(a.startswith("--fullscreen") or a.startswith("--fs-screen") for a in args)
    # the video ghost is still the exact opposite
    v = mpv_args(cfg, "t")
    assert "--mute=yes" in v and "--volume=0" in v and "--vid=no" not in v


def test_music_playing_on_this_pc_needs_no_ghost_and_no_pinned_output():
    cfg = Config({"engine": {"type": "none"}, "control": {"port": 0},
                  "sources": [{"source": "pc", "exe": "spotify.exe", "mode": "music",
                               "area_id": "a1"}]})
    pc = build_sources(cfg).sources[0]
    from hueghost.pcwatch import AudioHit
    pc.probe.observe(pc.bindings(), [AudioHit(9, "spotify.exe", 0.6)], None, 0.0)
    a = pc.activities(pc.probe.observe(pc.bindings(), [AudioHit(9, "spotify.exe", 0.6)], None, 2.0))[0]
    assert a.playing and a.needs_ghost is False and a.ghost is None
    # the sound is already on this PC: Hue Sync should use its own default input
    assert a.plan.mode == "music" and a.plan.audio_device == AUTO
