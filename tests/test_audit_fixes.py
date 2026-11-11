"""One test per bug the 2.13.5 audit found, so each fails if it comes back."""
import json
import os

import pytest

from hueghost import cli, ghost, screencare, winutil
from hueghost.config import Config
from hueghost.daemon import Daemon
from hueghost.jellyfin import JellyfinError
from hueghost.sources import FAILED_POLL_S, SourceSet
from hueghost.sources.jellyfin import JellyfinSource
from hueghost.winutil import Display


def test_doctor_runs_to_the_end(tmp_path, monkeypatch, capsys):
    class NoServer:
        def __init__(self, *a, **k):
            pass

        def public_info(self):
            raise JellyfinError("down")

    def no_daemon(*a, **k):
        raise OSError("not running")

    monkeypatch.setattr(cli, "JellyfinClient", NoServer)
    monkeypatch.setattr(cli, "list_displays", lambda: [
        Display(r"\\.\DISPLAY1", 0, 1920, 1080, 0, 0, True),
        Display(r"\\.\DISPLAY2", 1, 1920, 1080, 1920, 0, False)])
    monkeypatch.setattr(cli, "_probe_ws", lambda *a, **k: (False, None, "refused"))
    monkeypatch.setattr(cli, "_control_call", no_daemon)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"ghost": {"screen_name": r"\\.\DISPLAY2"}}))
    cli.main(["--config", str(cfg), "doctor"])       # used to raise NameError at the display check
    out = capsys.readouterr().out
    assert "[PASS] ghost display" in out and "result:" in out


def test_a_jellyfin_server_that_is_down_is_reported_and_polled_less():
    class Down:
        def sessions(self):
            raise JellyfinError("HTTP 401 for /Sessions")

    cfg = Config({"jellyfin": {"url": "http://jf", "api_key": "k"},
                  "sources": [{"source": "jellyfin", "device_id": "atv"}]})
    src = JellyfinSource(cfg, cfg.bindings(), client=Down())
    s = SourceSet([src])
    s.poll(0.0)
    assert src.ok is False and "401" in src.error       # used to be overwritten with ok=True
    assert src._next_poll == FAILED_POLL_S               # ... and asked again 0.5 s later


def test_the_ghost_and_the_covers_launch_the_mpv_that_was_found(monkeypatch):
    bundled = r"C:\Program Files\Hue Ghost\mpv\mpv.exe"
    for mod in (ghost, screencare):
        monkeypatch.setattr(mod, "find_mpv", lambda configured="": bundled)
    cfg = Config({})                                     # ghost.mpv_path is the default "mpv"
    assert ghost.mpv_args(cfg, "t", audio_only=True)[0] == bundled
    assert screencare.cover_args(cfg, 0)[0] == bundled


def test_no_token_means_no_lan(monkeypatch):
    seen = {}

    class FakeServer:
        def __init__(self, bind, *a, **k):
            seen["bind"] = bind

        def start(self):
            pass

    monkeypatch.setattr("hueghost.daemon.ControlServer", FakeServer)
    d = Daemon(Config({"engine": {"type": "none"}, "control": {"bind": "0.0.0.0", "port": 8787, "token": ""}}))
    try:
        d._start_control()
        assert seen["bind"] == "127.0.0.1"
    finally:
        d.engine.close()


def test_a_broken_config_file_is_set_aside_at_start(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"enabled": true,}')                   # the trailing comma of a hand edit
    with pytest.raises(ValueError):
        Config.load(str(p))                              # reload stays strict
    cfg = Config.load(str(p), recover=True)
    assert os.path.exists(str(p) + ".bad") and "could not be read" in cfg.problems()[0]
    cfg.save()
    assert cfg.load_error is None


def test_a_config_change_the_app_cannot_use_is_put_back(tmp_path):
    p = tmp_path / "config.json"
    cfg = Config({"engine": {"type": "none"}, "control": {"port": 0}, "jellyfin": {"api_key": "k"},
                  "sources": [{"source": "jellyfin", "device_id": "atv"}]}, str(p))
    d = Daemon(cfg)
    try:
        with pytest.raises(ValueError):
            d.apply_config({"jellyfin": {"poll_interval_s": "soon"}})
        assert d.cfg.get("jellyfin.poll_interval_s") == 0.5
        assert json.loads(p.read_text())["jellyfin"]["poll_interval_s"] == 0.5
    finally:
        d.engine.close()


def test_hue_sync_files_are_replaced_not_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(winutil, "hue_sync_dir", lambda: str(tmp_path))
    bridge = tmp_path / "bridge.json"
    bridge.write_text('{"SelectedGroup": "old", "key": "PEM"}')
    winutil.hue_sync_write_selected_area("new")
    assert bridge.read_text() == '{"SelectedGroup": "new", "key": "PEM"}'
    assert os.listdir(tmp_path) == ["bridge.json"]       # no temp file left behind


def test_setup_puts_its_pick_first_in_the_sources_the_app_reads():
    cfg = Config({"sources": [{"source": "jellyfin", "device_id": "old"},
                              {"source": "jellyfin", "device_id": "tv", "mode": "video", "intensity": "subtle"}]})
    pick = {"device_id": "tv", "client": ""}
    binds = cfg.bindings()
    same = next((b for b in binds if b["source"] == "jellyfin" and all(b[k] == v for k, v in pick.items())), None)
    cfg.set_bindings([same or dict(pick, source="jellyfin")] + [b for b in binds if b is not same])
    assert [b["device_id"] for b in cfg.bindings()] == ["tv", "old"]
    assert cfg.bindings()[0]["intensity"] == "subtle"    # its settings came along
