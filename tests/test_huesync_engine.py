import time

import pytest

from hueghost.engines import huesync as hs
from hueghost.engines.huesync import HueSyncEngine, build_command
from hueghost.wsclient import WebSocket
from tests.mock_huesync import MockHueSync


@pytest.fixture(autouse=True)
def fast_resend(monkeypatch):
    monkeypatch.setattr(hs, "RESEND_AFTER_S", 0.3)
    monkeypatch.setattr(hs, "BACKOFF_MIN_S", 0.1)
    monkeypatch.setattr(hs, "BACKOFF_MAX_S", 0.3)


@pytest.fixture
def app():
    m = MockHueSync()
    yield m
    m.close()


def wait(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_wsclient_handshake_and_roundtrip(app):
    ws = WebSocket("127.0.0.1", app.port, "/").connect()
    assert ws.server_header == "Hue Sync Public Control"
    assert '"app_state_update"' in ws.recv_text()
    ws.send_text(build_command("inc_bri", step=4))
    assert '"bri": 60' in ws.recv_text()
    assert app.received[-1] == {"command": "inc_bri", "data": {"step": 4}}
    ws.close()


def test_command_shapes():
    assert build_command("start_sync") == '{"command": "start_sync"}'
    assert build_command("inc_bri", step=-5) == '{"command": "inc_bri", "data": {"step": -5}}'


def test_engine_reads_state_and_starts_stops_sync(app):
    eng = HueSyncEngine("127.0.0.1", app.port, mode="video", intensity="high")
    try:
        assert wait(lambda: eng.state().connected and eng.state().state == "bridge_connected")
        eng.start()
        assert wait(lambda: eng.state().syncing)
        assert app.state == "syncing"
        assert app.commands() == ["start_sync"]           # mode/intensity already matched -> nothing else sent
        eng.stop()
        assert wait(lambda: not eng.state().syncing)
        assert app.state == "bridge_connected"
        assert app.commands() == ["start_sync", "stop_sync"]
    finally:
        eng.close()


def test_engine_reconciles_sync_then_mode_then_intensity(app):
    app.mode, app.intensity = "games", "subtle"
    eng = HueSyncEngine("127.0.0.1", app.port, mode="video", intensity="extreme")
    try:
        eng.start()
        assert wait(lambda: eng.state().syncing and app.mode == "video" and app.intensity == "extreme", 5.0)
        assert app.commands() == ["start_sync", "set_app_mode", "set_intensity"]
        assert app.received[1]["data"] == {"mode": "video"}
        assert app.received[2]["data"] == {"intensity": "extreme"}
    finally:
        eng.close()


def test_engine_reasserts_after_app_restart(app):
    eng = HueSyncEngine("127.0.0.1", app.port, mode="video", intensity="high")
    try:
        eng.start()
        assert wait(lambda: eng.state().syncing)
        # "kill" Hue Sync: drop the socket and come back not syncing
        app.state = "bridge_connected"
        app.drop_clients()
        assert wait(lambda: app.connections >= 2 and eng.state().syncing, 5.0)
        assert app.commands().count("start_sync") == 2
    finally:
        eng.close()


def test_engine_tolerates_unreachable_app_and_unknown_events():
    eng = HueSyncEngine("127.0.0.1", 1, mode="video", intensity="high")   # nothing listens on port 1
    try:
        eng.start()
        assert wait(lambda: eng.state().error is not None, 6.0)
        st = eng.state()
        assert not st.connected and st.desired_sync and "unreachable" in (st.error or "")
    finally:
        eng.close()
    eng2 = HueSyncEngine("127.0.0.1", 1)
    eng2._handle('{"event":"something_new","data":{}}')
    eng2._handle("not json")
    assert eng2.state().state is None
    eng2.close()


def test_engine_reports_bridge_disconnected(app):
    app.state = "bridge_disconnected"
    eng = HueSyncEngine("127.0.0.1", app.port)
    try:
        eng.start()
        assert wait(lambda: eng.state().error == "Hue Sync is not connected to a bridge")
        assert app.commands() == []
    finally:
        eng.close()


def test_set_intensity_live_and_brightness(app):
    eng = HueSyncEngine("127.0.0.1", app.port, mode="video", intensity="high")
    try:
        assert wait(lambda: eng.state().connected)
        eng.start()
        assert wait(lambda: eng.state().syncing)
        eng.set_intensity("moderate")
        assert wait(lambda: app.intensity == "moderate")
        eng.adjust_brightness(-6)
        assert wait(lambda: app.bri == 50)
        with pytest.raises(ValueError):
            eng.set_intensity("max")
    finally:
        eng.close()
