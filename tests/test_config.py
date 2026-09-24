import json

from hueghost.config import Config, migrate_legacy, is_legacy

LEGACY = {
    "server_url": "http://192.168.0.156:8096", "api_key": "k", "enabled": True, "control_port": 8787,
    "watch_device": "Apple TV", "watch_user": "", "poll_interval_s": 1.0, "seek_threshold_s": 1.0,
    "nudge_threshold_s": 0.25, "max_speed_delta": 0.04, "sync_offset_s": 1.5, "seek_cooldown_s": 3.0,
    "idle_stop_delay_s": 10.0, "relaunch_cooldown_s": 5.0, "relaunch_max_per_5min": 6,
    "mpv_path": "C:\\Program Files\\MPV Player\\mpv.exe", "ghost_fullscreen": True,
    "ghost_geometry": "28%x28%-40-40", "ipc_pipe": "\\\\.\\pipe\\hue-ghost", "huestacean_url": "",
    "log_level": "INFO",
}


def test_legacy_detection_and_migration():
    assert is_legacy(LEGACY)
    cfg = Config(migrate_legacy(LEGACY))
    assert cfg.get("jellyfin.url") == "http://192.168.0.156:8096"
    assert cfg.get("jellyfin.api_key") == "k"
    assert cfg.get("jellyfin.follow.device_name_contains") == "Apple TV"
    assert cfg.get("sync.offset_s") == 1.5
    assert cfg.get("ghost.mpv_path").endswith("mpv.exe")
    assert cfg.get("ghost.fullscreen") is True
    assert cfg.get("engine.type") == "huesync"          # empty huestacean_url -> default engine
    assert cfg.get("control.port") == 8787
    assert cfg.problems() == []


def test_legacy_huestacean_becomes_httphook():
    cfg = Config(migrate_legacy({**LEGACY, "huestacean_url": "http://127.0.0.1:8989"}))
    assert cfg.get("engine.type") == "httphook"
    assert cfg.get("engine.httphook.url") == "http://127.0.0.1:8989"


def test_load_save_roundtrip(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(LEGACY), encoding="utf-8")
    cfg = Config.load(str(p))
    assert cfg.migrated
    cfg.set("sync.offset_s", 2.25)
    cfg.save()
    again = Config.load(str(p))
    assert not again.migrated
    assert again.get("sync.offset_s") == 2.25
    assert again.get("jellyfin.follow.device_name_contains") == "Apple TV"


def test_defaults_and_ipc_platform_default():
    cfg = Config({})
    assert cfg.get("jellyfin.poll_interval_s") == 0.5
    assert cfg.get("ghost.ipc")           # platform default filled in
    assert "nothing to follow" in " ".join(cfg.problems())


def test_a_jellyfin_server_is_only_required_when_something_follows_it():
    # a PC-only install is a complete setup: no server, no API key, still valid
    pc = Config({"sources": [{"source": "pc", "exe": "eldenring.exe", "mode": "games",
                              "area_id": "a1"}]})
    assert pc.problems() == []
    assert pc.bindings()[0]["detect"] == "fullscreen"      # default for a game

    jf = Config({"sources": [{"source": "jellyfin", "device_id": "atv"}]})
    assert "api_key" in " ".join(jf.problems())      # the url has a default; the key has none
    jf.set("jellyfin.url", "")
    assert "jellyfin.url" in " ".join(jf.problems())


def test_bindings_round_trip_and_keep_the_legacy_players_mirrored():
    cfg = Config({"jellyfin": {"url": "http://x", "api_key": "k"}})
    cfg.set_bindings([
        {"source": "jellyfin", "device_id": "atv", "name": "Apple TV", "area_id": "a1", "area_name": "Living"},
        {"source": "pc", "exe": "Firefox.exe", "mode": "video", "area_id": "a2", "enabled": False},
        {"source": "jellyfin", "device_name_contains": "Office", "area_id": "a3"},
    ])
    ids = [b["id"] for b in cfg.bindings()]
    assert ids == ["apple-tv", "firefox-exe", "office"]
    assert cfg.bindings()[1]["exe"] == "firefox.exe"        # matched case-insensitively
    assert [b["id"] for b in cfg.enabled_bindings()] == ["apple-tv", "office"]
    # the pre-2.4 keys still describe the Jellyfin half, for a downgrade
    assert cfg.get("jellyfin.follow.device_id") == "atv"
    assert cfg.get("jellyfin.follow_area_id") == "a1"
    assert [p["device_name_contains"] for p in cfg.get("jellyfin.players")] == ["Office"]


def test_duplicate_binding_ids_are_made_unique():
    cfg = Config({})
    cfg.set_bindings([{"source": "pc", "exe": "mpv.exe"}, {"source": "pc", "exe": "mpv.exe"}])
    assert [b["id"] for b in cfg.bindings()] == ["mpv-exe", "mpv-exe-2"]


def test_music_binding_without_an_output_says_so():
    # the ghost has to play the track somewhere; with nowhere silent configured
    # that somewhere would be the speakers, so the binding is refused instead
    cfg = Config({"jellyfin": {"url": "http://x", "api_key": "k"}})
    music = {"source": "jellyfin", "device_id": "atv", "kinds": ["music"]}
    assert "music needs an output" in " ".join(cfg.binding_problems(music))
    cfg.set("ghost.audio_device", "{0.0.0.00000000}.{cable}")
    assert cfg.binding_problems(music) == []
    # a video-only binding never needs one
    assert cfg.binding_problems({"source": "jellyfin", "device_id": "atv", "kinds": ["video"]}) == []


def test_a_binding_keeps_its_own_mode_and_intensity():
    """Each source is set up once and then simply played, so its mode and
    intensity have to survive the round trip like any other field."""
    cfg = Config({"jellyfin": {"url": "http://x", "api_key": "k"}})
    cfg.set_bindings([
        {"source": "jellyfin", "device_id": "atv", "name": "Apple TV",
         "mode": "video", "intensity": "subtle"},
        {"source": "jellyfin", "device_id": "s23", "client": "CAMusic", "name": "S23 music",
         "mode": "music", "intensity": "high"},
        {"source": "pc", "exe": "spotify.exe", "mode": "MUSIC", "intensity": "Extreme"},
    ])
    got = cfg.bindings()
    assert [(b["mode"], b["intensity"]) for b in got] == [
        ("video", "subtle"), ("music", "high"), ("music", "extreme")]


def test_a_binding_with_no_settings_of_its_own_leaves_them_empty():
    # "" means "whatever the global setting says"; only a PC app must declare a
    # mode, because nothing else can tell a film from a game
    cfg = Config({})
    cfg.set_bindings([{"source": "jellyfin", "device_id": "atv"},
                      {"source": "pc", "exe": "vlc.exe"},
                      {"source": "jellyfin", "device_id": "tv", "mode": "nonsense",
                       "intensity": "blinding"}])
    got = cfg.bindings()
    assert [(b["mode"], b["intensity"]) for b in got] == [("", ""), ("video", ""), ("", "")]


def test_reload_picks_up_changes(tmp_path):
    p = tmp_path / "config.json"
    cfg = Config({"jellyfin": {"api_key": "a"}}, str(p))
    cfg.save()
    p.write_text(json.dumps({"jellyfin": {"api_key": "b"}, "sync": {"offset_s": 9}}), encoding="utf-8")
    cfg.reload()
    assert cfg.get("jellyfin.api_key") == "b"
    assert cfg.get("sync.offset_s") == 9
    assert cfg.get("sync.seek_threshold_s") == 1.0   # defaults still merged


def test_a_binding_keeps_the_app_it_was_added_for():
    """The app is part of a Jellyfin binding's identity, so it has to survive a
    save/load round trip like any other field."""
    from hueghost.config import Config
    c = Config({})
    c.set_bindings([
        {"source": "jellyfin", "device_id": "id-music", "client": "CAMusic", "name": "S23 music"},
        {"source": "jellyfin", "device_id": "id-video", "client": "Jellyfin for Android", "name": "S23 films"},
    ])
    got = c.bindings()
    assert [b["client"] for b in got] == ["CAMusic", "Jellyfin for Android"]
    assert [b["name"] for b in got] == ["S23 music", "S23 films"]
    # two entries for one phone must not collapse into one id
    assert got[0]["id"] != got[1]["id"]
