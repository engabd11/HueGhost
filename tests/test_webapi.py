"""The settings API the desktop app (and Home Assistant) talk to, against a
daemon built on a stub Jellyfin and the null engine - no mpv, no network."""
import json
import urllib.request

import pytest

from hueghost.config import Config
from hueghost.daemon import Daemon
from hueghost.webapi import ApiError, WebApi


@pytest.fixture
def daemon(tmp_path):
    cfg = Config({"jellyfin": {"url": "http://127.0.0.1:1", "api_key": "k", "follow": {"device_name_contains": "tv"}},
                  "engine": {"type": "none"}, "control": {"port": 0}}, str(tmp_path / "config.json"))
    cfg.save()
    d = Daemon(cfg)
    yield d
    d.engine.close()


def test_config_apply_saves_and_reports_restart_needs(daemon, tmp_path):
    api = WebApi(daemon)
    res = api.handle("POST", "/api/config", {"sync": {"offset_s": 2.25}, "engine": {"huesync": {"intensity": "subtle"}}}, {})
    assert res["ok"] and res["restart_required"] == []
    assert daemon.cfg.get("sync.offset_s") == 2.25
    saved = json.load(open(tmp_path / "config.json", encoding="utf-8"))
    assert saved["sync"]["offset_s"] == 2.25 and saved["engine"]["huesync"]["intensity"] == "subtle"
    res = api.handle("POST", "/api/config", {"control": {"bind": "0.0.0.0", "token": "t"}}, {})
    assert res["restart_required"] == ["control"]
    assert daemon.status()["restart_required"] == ["control"]


def test_config_apply_rebuilds_watcher_but_keeps_learned_stalls(daemon):
    daemon.watcher.stalls.learn("seek", 6.0)
    before = daemon.watcher.stalls.predict("seek")
    WebApi(daemon).handle("POST", "/api/config", {"jellyfin": {"follow": {"device_name_contains": "other"}}}, {})
    assert daemon.watcher.matcher.needle == "other"
    assert daemon.watcher.stalls.predict("seek") == before


def test_engine_rebuild_on_type_change(daemon):
    api = WebApi(daemon)
    api.handle("POST", "/api/config", {"engine": {"type": "httphook", "httphook": {"url": "http://127.0.0.1:1"}}}, {})
    assert daemon.engine.name == "httphook"
    api.handle("POST", "/api/config", {"engine": {"type": "none"}}, {})
    assert daemon.engine.name == "none"


def test_setup_required_surfaces_in_status(tmp_path):
    cfg = Config({"engine": {"type": "none"}, "control": {"port": 0}}, str(tmp_path / "c.json"))
    d = Daemon(cfg)
    try:
        st = d.status()
        assert st["setup_required"] and "nothing to follow" in " ".join(st["setup_required"])
        d._poll(0.0)                     # must not raise or touch Jellyfin
        assert d.jf_error.startswith("setup required")
    finally:
        d.engine.close()


def test_sessions_errors_are_api_errors(daemon):
    api = WebApi(daemon)
    with pytest.raises(ApiError):
        api.handle("GET", "/api/sessions", {}, {"url": "http://127.0.0.1:1", "api_key": "x"})
    with pytest.raises(ApiError) as e:
        api.handle("GET", "/api/nope", {}, {})
    assert e.value.code == 404


def test_system_and_stall_reset(daemon):
    api = WebApi(daemon)
    info = api.handle("GET", "/api/system", {}, {})
    assert info["version"] and "subtle" in info["intensities"]
    daemon.watcher.stalls.learn("seek", 9.0)
    res = api.handle("POST", "/api/stalls/reset", {}, {})
    assert res["stall_estimates"]["seek"] == 3.5


def test_api_served_over_http_with_token(tmp_path):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cfg = Config({"jellyfin": {"url": "http://127.0.0.1:1", "api_key": "k", "follow": {"device_name_contains": "tv"}},
                  "engine": {"type": "none"}, "control": {"port": port, "token": "sekret"}}, str(tmp_path / "c.json"))
    d = Daemon(cfg)
    d._start_control()
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/system" % port, headers={"Authorization": "Bearer sekret"})
        with urllib.request.urlopen(req, timeout=3) as r:
            assert json.loads(r.read())["version"]
        req = urllib.request.Request("http://127.0.0.1:%d/api/config" % port, data=json.dumps({"sync": {"offset_s": 0.5}}).encode(),
                                     headers={"Authorization": "Bearer sekret", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            assert json.loads(r.read())["ok"]
        assert d.cfg.get("sync.offset_s") == 0.5
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen("http://127.0.0.1:%d/api/system" % port, timeout=3)
        assert e.value.code == 401
    finally:
        d.control.stop()
        d.engine.close()
def test_set_mode_and_audio_switch_round_trip(daemon, tmp_path):
    api = WebApi(daemon)
    api.handle("POST", "/api/set", {"mode": "music", "intensity": "subtle"}, {})
    st = daemon.status()
    assert st["mode"] == "music" and st["intensity"] == "subtle"
    with pytest.raises(ValueError):
        api.handle("POST", "/api/set", {"mode": "disco"}, {})
    assert daemon.status()["mode"] == "music"

    # "use audio for light effects": on / off / leave it to the app
    for sent, stored in ((True, True), (False, False), (None, None), ("auto", None), ("on", True)):
        api.handle("POST", "/api/set", {"use_audio": sent}, {})
        assert daemon.cfg.get("engine.huesync.use_audio") is stored, sent
    assert daemon.status()["use_audio"] is True
    saved = json.load(open(tmp_path / "config.json", encoding="utf-8"))
    assert saved["engine"]["huesync"]["mode"] == "music"


def test_mode_and_intensity_lists_are_published_for_clients(daemon):
    api = WebApi(daemon)
    assert api.handle("GET", "/api/modes", {}, {}) == {
        "modes": ["video", "music", "games"], "intensities": ["subtle", "moderate", "high", "extreme"]}
    assert api.handle("GET", "/api/system", {}, {})["modes"] == ["video", "music", "games"]
