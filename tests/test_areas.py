"""Players bound to entertainment areas: priority, area hand-over, and the
engine's stop/kill/patch/relaunch switch (OS actions stubbed)."""
import time

import pytest

from hueghost.config import Config
from hueghost.engines import huesync as hs
from hueghost.engines.huesync import HueSyncEngine
from hueghost.watcher import PlayerSet, SessionWatcher
from tests.mock_huesync import MockHueSync
from tests.test_watcher import LOCAL0, MONO0, session


def sess(device_id, name, playing=True, pos=10.0):
    s = session(pos, False, LOCAL0 + 5)
    s["DeviceId"], s["DeviceName"] = device_id, name
    if not playing:
        s.pop("NowPlayingItem")
        s["PlayState"] = {}
    return s


PLAYERS = [
    {"device_id": "atv", "device_name_contains": "Apple TV", "user": "", "area_id": "area-living", "area_name": "Living room"},
    {"device_id": "", "device_name_contains": "office", "user": "", "area_id": "area-office", "area_name": "Office"},
    {"device_id": "phone", "device_name_contains": "", "user": "", "area_id": "", "area_name": ""},
]


def test_config_players_roundtrip_and_legacy_follow():
    cfg = Config({"jellyfin": {"follow": {"device_name_contains": "Apple TV"}}})
    assert cfg.players() == [{"device_id": "", "device_name_contains": "Apple TV", "user": "", "area_id": "", "area_name": ""}]
    cfg.set_players(PLAYERS)
    assert cfg.get("jellyfin.follow.device_id") == "atv" and cfg.get("jellyfin.follow_area_id") == "area-living"
    assert len(cfg.get("jellyfin.players")) == 2
    assert cfg.players() == PLAYERS
    cfg.set_players([])
    assert cfg.players() == [] and "no player" in cfg.problems()[-1]


def test_first_playing_player_wins_in_priority_order():
    ps = PlayerSet.from_players(PLAYERS)
    living, office, phone = sess("atv", "Apple TV"), sess("x1", "Office TV"), sess("phone", "S23")
    s, who = ps.pick([phone, office])
    assert s is office and who.area_id == "area-office"
    s, who = ps.pick([phone, office, living])
    assert s is living and who.area_id == "area-living"
    s, who = ps.pick([sess("phone", "S23", playing=False)])
    assert s is not None and who.area_id is None                   # seen but idle
    assert ps.pick([sess("zzz", "Kitchen")]) == (None, None)


def test_watcher_switches_model_when_the_playing_device_changes():
    w = SessionWatcher(PlayerSet.from_players(PLAYERS))
    obs = w.observe([sess("atv", "Apple TV", pos=100.0)], LOCAL0 + 1, LOCAL0 + 1, MONO0 + 1)
    assert obs.event == "new_item" and obs.player.area_name == "Living room"
    obs = w.observe([sess("atv", "Apple TV", pos=100.5)], LOCAL0 + 2, LOCAL0 + 2, MONO0 + 2)
    assert obs.event is None
    # living room stops, office starts the same item id -> new model + office area
    obs = w.observe([sess("x1", "Office TV", pos=100.0)], LOCAL0 + 3, LOCAL0 + 3, MONO0 + 3)
    assert obs.event == "new_item" and obs.player.area_name == "Office"


class SwitchableEngine(HueSyncEngine):
    """HueSyncEngine with the OS side replaced by a scripted fake app."""

    def __init__(self, app: MockHueSync, area="area-office"):
        self.fake_area = area
        self.ops: list[str] = []
        self.app = app
        super().__init__("127.0.0.1", app.port, mode="video", intensity="")

    def _read_area(self):
        return self.fake_area, {"area-office": "Office", "area-living": "Living room"}.get(self.fake_area)

    def _write_area(self, area_id):
        self.ops.append("write:" + area_id)
        self.fake_area = area_id

    def _kill_app(self):
        self.ops.append("kill")
        self.app.state = "bridge_connected"
        self.app.drop_clients()
        return True

    def _launch_app(self):
        self.ops.append("launch")
        return True

    def areas(self):
        return [{"id": "area-office", "name": "Office"}, {"id": "area-living", "name": "Living room"}]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(hs, "RESEND_AFTER_S", 0.3)
    monkeypatch.setattr(hs, "BACKOFF_MIN_S", 0.1)
    monkeypatch.setattr(hs, "BACKOFF_MAX_S", 0.3)


def wait(pred, timeout=5.0):
    t = time.time() + timeout
    while time.time() < t:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_engine_switches_area_then_syncs():
    app = MockHueSync()
    eng = SwitchableEngine(app, area="area-office")
    try:
        assert wait(lambda: eng.state().connected and eng.state().area_name == "Office")
        eng.set_area("area-living")
        eng.start()
        assert wait(lambda: eng.state().syncing and eng.state().area_id == "area-living", 8.0)
        assert eng.ops == ["kill", "write:area-living", "launch"]
        assert app.connections >= 2 and app.commands().count("start_sync") == 1
        # same area again: nothing happens
        eng.set_area("area-living")
        time.sleep(0.6)
        assert eng.ops == ["kill", "write:area-living", "launch"]
    finally:
        eng.close()
        app.close()


def test_engine_stops_its_own_sync_before_switching_and_leaves_manual_syncs_alone():
    app = MockHueSync()
    eng = SwitchableEngine(app, area="area-office")
    try:
        assert wait(lambda: eng.state().connected)
        # a sync started by hand in the app (not by us) is not touched
        app.state = "syncing"
        app.broadcast_state()
        time.sleep(0.8)
        assert "stop_sync" not in app.commands() and app.state == "syncing"
        # ours: start, then switch -> stop first
        app.state = "bridge_connected"
        app.broadcast_state()
        assert wait(lambda: eng.state().state == "bridge_connected")
        eng.start()
        assert wait(lambda: eng.state().syncing)
        eng.set_area("area-living")
        assert wait(lambda: "write:area-living" in eng.ops, 8.0)
        cmds = app.commands()
        assert cmds.index("stop_sync") > cmds.index("start_sync")
        assert wait(lambda: eng.state().syncing and eng.state().area_id == "area-living", 8.0)
        eng.stop()
        assert wait(lambda: not eng.state().syncing)
    finally:
        eng.close()
        app.close()
