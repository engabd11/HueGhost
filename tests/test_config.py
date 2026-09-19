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
    assert "api_key" in " ".join(cfg.problems())


def test_reload_picks_up_changes(tmp_path):
    p = tmp_path / "config.json"
    cfg = Config({"jellyfin": {"api_key": "a"}}, str(p))
    cfg.save()
    p.write_text(json.dumps({"jellyfin": {"api_key": "b"}, "sync": {"offset_s": 9}}), encoding="utf-8")
    cfg.reload()
    assert cfg.get("jellyfin.api_key") == "b"
    assert cfg.get("sync.offset_s") == 9
    assert cfg.get("sync.seek_threshold_s") == 1.0   # defaults still merged
