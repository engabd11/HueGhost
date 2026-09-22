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
    assert cfg.players() == [] and "nothing to follow" in " ".join(cfg.problems())


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

    def __init__(self, app: MockHueSync, area="area-office", audio=False,
                 monitor="MONITOR\\VDD\\0002", audio_device="{0.0.0.0}.{spk}", **kw):
        self.fake_area = area
        self.fake_audio = {"video": audio, "games": audio}
        self.fake_monitor = monitor
        self.fake_audio_device = audio_device
        self.ops: list[str] = []
        self.app = app
        super().__init__("127.0.0.1", app.port, mode="video", intensity="", **kw)

    def _read_area(self):
        return self.fake_area, {"area-office": "Office", "area-living": "Living room"}.get(self.fake_area)

    def _write_area(self, area_id):
        self.ops.append("write:" + area_id)
        self.fake_area = area_id

    def _read_use_audio(self, mode):
        return self.fake_audio.get(mode)

    def _write_use_audio(self, mode, on):
        self.ops.append("audio:%s=%s" % (mode, "on" if on else "off"))
        self.fake_audio[mode] = on

    def _read_monitor(self):
        return self.fake_monitor

    def _write_monitor(self, monitor):
        self.ops.append("monitor:" + monitor)
        self.fake_monitor = monitor

    def _read_audio_device(self):
        return self.fake_audio_device

    def _write_audio_device(self, endpoint):
        self.ops.append("audiodev:" + endpoint)
        self.fake_audio_device = endpoint

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
    monkeypatch.setattr(hs, "APP_READ_EVERY_S", 0.3)


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


def test_every_startup_only_setting_lands_in_one_restart():
    from hueghost.engines import Plan
    app = MockHueSync()
    eng = SwitchableEngine(app, area="area-office", audio=False)
    try:
        assert wait(lambda: eng.state().connected and eng.state().monitor_id == "MONITOR\\VDD\\0002")
        # a PC video binding: another area, the real display, audio effects on
        eng.apply_plan(Plan(area_id="area-living", mode="video", monitor="MONITOR\\REAL\\0001",
                            use_audio=True))
        eng.start()
        assert wait(lambda: eng.state().syncing and eng.state().area_id == "area-living", 8.0)
        assert eng.ops == ["kill", "write:area-living", "audio:video=on",
                           "monitor:MONITOR\\REAL\\0001", "launch"]
        assert eng.ops.count("kill") == 1 and eng.ops.count("launch") == 1
        assert eng.state().monitor_id == "MONITOR\\REAL\\0001"
        # settled: re-asserting the same wishes restarts nothing
        eng.apply_plan(Plan(area_id="area-living", mode="video", monitor="MONITOR\\REAL\\0001",
                            use_audio=True))
        time.sleep(0.8)
        assert eng.ops.count("kill") == 1
    finally:
        eng.close()
        app.close()


def test_a_display_the_user_picks_in_the_app_is_not_fought():
    from hueghost.engines import Plan
    app = MockHueSync()
    eng = SwitchableEngine(app, area="area-office")
    try:
        assert wait(lambda: eng.state().connected)
        eng.apply_plan(Plan(area_id="area-office", mode="video", monitor="MONITOR\\VDD\\0002"))
        eng.start()
        assert wait(lambda: eng.state().syncing, 8.0)
        assert eng.ops == []                       # already where we want it
        # the user picks another display in the app, mid-session
        eng.fake_monitor = "MONITOR\\OTHER\\0003"
        time.sleep(0.8)
        assert eng.ops == []                       # ours was applied once; theirs stands
        assert wait(lambda: eng.state().monitor_id == "MONITOR\\OTHER\\0003")
    finally:
        eng.close()
        app.close()


def test_manage_monitor_off_leaves_the_display_alone():
    from hueghost.engines import Plan
    app = MockHueSync()
    eng = SwitchableEngine(app, area="area-office", manage_monitor=False)
    try:
        assert wait(lambda: eng.state().connected)
        eng.apply_plan(Plan(area_id="area-office", mode="video", monitor="MONITOR\\REAL\\0001"))
        eng.start()
        assert wait(lambda: eng.state().syncing, 8.0)
        time.sleep(0.5)
        assert eng.ops == [] and eng.fake_monitor == "MONITOR\\VDD\\0002"
    finally:
        eng.close()
        app.close()


def test_music_never_drags_the_capture_display_along():
    from hueghost.engines import Plan
    app = MockHueSync(mode="music")
    eng = SwitchableEngine(app, area="area-office")
    try:
        assert wait(lambda: eng.state().connected)
        # a Jellyfin-music session: pin the endpoint the ghost plays into, but
        # music mode captures no screen, so the display must not cause a restart
        eng.apply_plan(Plan(area_id="area-office", mode="music", monitor="MONITOR\\REAL\\0001",
                            audio_device="{0.0.0.0}.{cable}"))
        eng.start()
        assert wait(lambda: "audiodev:{0.0.0.0}.{cable}" in eng.ops, 8.0)
        assert not any(o.startswith("monitor:") for o in eng.ops)
        assert eng.ops.count("kill") == 1
    finally:
        eng.close()
        app.close()


def test_brightness_survives_a_closed_app_and_lands_as_a_step():
    app = MockHueSync(bri=56)
    eng = SwitchableEngine(app, area="area-office")
    try:
        assert wait(lambda: eng.state().connected and eng.state().bri == 56)
        eng.set_brightness(30)             # absolute: the protocol only has a step
        assert wait(lambda: app.bri == 30, 5.0)          # 56 - 26, computed from the app's own level
        assert app.commands().count("inc_bri") == 1
        eng.adjust_brightness(-10)
        assert wait(lambda: app.bri == 20, 5.0)
    finally:
        eng.close()
        app.close()


def test_brightness_with_no_connection_is_not_an_error():
    # the LAN API used to turn this into an HTTP 500 when Hue Sync was closed
    eng = HueSyncEngine("127.0.0.1", 1, mode="video")
    try:
        eng.adjust_brightness(10)
        eng.set_brightness(40)
    finally:
        eng.close()


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


def test_apply_config_rebuilds_watcher_even_if_live_config_was_mutated_first(tmp_path):
    from hueghost.daemon import Daemon
    cfg = Config({"jellyfin": {"url": "http://127.0.0.1:1", "api_key": "k", "follow": {"device_id": "old"}},
                  "engine": {"type": "none"}, "control": {"port": 0}}, str(tmp_path / "c.json"))
    d = Daemon(cfg)
    try:
        assert d.watcher.players.matchers[0].device_id == "old"
        # the GUI used to mutate cfg before calling apply_config
        d.cfg.set_players(PLAYERS)
        d.apply_config({"jellyfin": {"follow": d.cfg.get("jellyfin.follow"), "players": d.cfg.get("jellyfin.players"),
                                     "follow_area_id": d.cfg.get("jellyfin.follow_area_id")}})
        ids = [m.device_id or m.needle for m in d.watcher.players.matchers]
        assert ids == ["atv", "office", "phone"]
        assert d.watcher.players.matchers[0].area_name == "Living room"
    finally:
        d.engine.close()


def test_matcher_accepts_device_id_or_name():
    from hueghost.watcher import SessionMatcher
    m = SessionMatcher(device_id="old-id", name_contains="Apple TV")
    assert m.matches({"DeviceId": "old-id", "DeviceName": "x"})
    assert m.matches({"DeviceId": "new-id", "DeviceName": "Apple TV", "Client": "Moonfin"})   # id regenerated
    assert not m.matches({"DeviceId": "new-id", "DeviceName": "AppleTV"})


# -- the app belongs to the user unless we are actually syncing -------------------------
def test_idle_engine_never_touches_the_area_the_user_picks_in_the_app():
    """The reported crash: changing the area in Hue Sync while Hue Ghost idled
    got the app killed and put back on Hue Ghost's area."""
    app = MockHueSync()
    eng = SwitchableEngine(app, area="area-office")
    try:
        assert wait(lambda: eng.state().connected)
        eng.set_area("area-living")          # bound player wants the living room...
        time.sleep(1.0)                      # ...but nothing is playing
        assert eng.ops == []
        # the user now picks another area in the app itself
        eng.fake_area = "area-living"
        assert wait(lambda: eng.state().area_name == "Living room", 12.0)
        assert eng.ops == []
    finally:
        eng.close()
        app.close()


def test_an_area_change_made_in_the_app_mid_session_is_adopted_not_fought():
    app = MockHueSync()
    eng = SwitchableEngine(app, area="area-office")
    try:
        assert wait(lambda: eng.state().connected)
        eng.set_area("area-office")          # already there: no restart needed
        eng.start()
        assert wait(lambda: eng.state().syncing)
        assert eng.ops == []
        eng.fake_area = "area-living"        # user changes it in Hue Sync
        time.sleep(1.2)
        assert eng.ops == []                 # app not killed
        assert eng.state().syncing           # and our sync carries on
        assert eng.state().area_name == "Living room"   # ... reporting where the lights are
        # ... but the next session puts our own area back
        eng.stop()
        assert wait(lambda: not eng.state().syncing)
        eng.start()
        assert wait(lambda: "write:area-office" in eng.ops, 8.0)
    finally:
        eng.close()
        app.close()


def test_manage_area_off_leaves_the_area_alone_even_while_syncing():
    app = MockHueSync()
    eng = SwitchableEngine(app, area="area-office", manage_area=False)
    try:
        assert wait(lambda: eng.state().connected)
        eng.set_area("area-living")
        eng.start()
        assert wait(lambda: eng.state().syncing, 8.0)
        time.sleep(0.8)
        assert eng.ops == [] and eng.state().area_id == "area-office"
    finally:
        eng.close()
        app.close()


# -- "use audio for light effects" ------------------------------------------------------
def test_audio_switch_and_area_are_applied_in_one_restart():
    app = MockHueSync()
    eng = SwitchableEngine(app, area="area-office", audio=False)
    try:
        assert wait(lambda: eng.state().connected)
        eng.set_area("area-living")
        eng.set_use_audio(True)
        eng.start()
        assert wait(lambda: eng.state().syncing and eng.state().area_id == "area-living", 8.0)
        assert eng.ops == ["kill", "write:area-living", "audio:video=on", "launch"]
        assert eng.state().use_audio is True
    finally:
        eng.close()
        app.close()


def test_audio_switch_alone_restarts_the_app_once_per_session():
    app = MockHueSync()
    eng = SwitchableEngine(app, area="area-office", audio=True)
    try:
        assert wait(lambda: eng.state().connected)
        eng.set_use_audio(False)
        eng.start()
        assert wait(lambda: "audio:video=off" in eng.ops, 8.0)
        assert eng.ops == ["kill", "audio:video=off", "launch"]
        assert wait(lambda: eng.state().syncing, 8.0)
        time.sleep(0.8)
        assert eng.ops.count("kill") == 1
        # the user turns it back on in the app: not fought
        eng.fake_audio["video"] = True
        time.sleep(1.2)
        assert eng.ops.count("kill") == 1
    finally:
        eng.close()
        app.close()


def test_audio_none_means_leave_the_apps_own_setting_alone():
    app = MockHueSync()
    eng = SwitchableEngine(app, area="area-office", audio=True)
    try:
        assert wait(lambda: eng.state().connected)
        eng.set_use_audio(None)
        eng.start()
        assert wait(lambda: eng.state().syncing, 8.0)
        time.sleep(0.8)
        assert eng.ops == [] and eng.fake_audio["video"] is True
    finally:
        eng.close()
        app.close()


def test_music_mode_has_no_audio_switch_to_apply():
    app = MockHueSync()
    eng = SwitchableEngine(app, area="area-office", audio=False)
    try:
        assert wait(lambda: eng.state().connected)
        eng.set_mode("music")
        eng.set_use_audio(True)
        eng.start()
        assert wait(lambda: eng.state().syncing, 8.0)
        time.sleep(0.8)
        assert eng.ops == []
        assert wait(lambda: eng.state().mode == "music", 5.0)
    finally:
        eng.close()
        app.close()
