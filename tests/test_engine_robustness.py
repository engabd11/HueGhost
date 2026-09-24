"""Regression tests: the light-engine worker thread must survive crashes.

A dead engine thread is the worst failure mode Hue Ghost has: the ghost keeps
playing, the daemon looks alive, but no start_sync / set_intensity is ever
re-asserted (seen live 2026-09-19: an area switch crashed the worker and sync
never came back until the app was restarted).
"""
import time

import pytest

from hueghost.daemon import pause_stop_due
from hueghost.engines import huesync as hs
from hueghost.engines.huesync import HueSyncEngine
from tests.mock_huesync import MockHueSync


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(hs, "RESEND_AFTER_S", 0.3)
    monkeypatch.setattr(hs, "BACKOFF_MIN_S", 0.1)
    monkeypatch.setattr(hs, "BACKOFF_MAX_S", 0.3)


@pytest.fixture
def app():
    m = MockHueSync()
    yield m
    m.close()


def wait(pred, timeout=6.0):
    t = time.time() + timeout
    while time.time() < t:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_reconcile_crash_keeps_connection(app):
    eng = HueSyncEngine("127.0.0.1", app.port, mode="video", intensity="")
    orig = eng._reconcile
    calls = {"n": 0}

    def flaky(ws):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("reconcile boom")
        orig(ws)

    eng._reconcile = flaky
    try:
        eng.start()
        assert wait(lambda: eng.state().syncing)
        assert eng.alive() and app.connections == 1      # no reconnect needed
    finally:
        eng.close()
        app.close()


def test_a_reconcile_on_a_dead_socket_reconnects_quietly(app):
    """A reconcile that restarts the app closes the connection on purpose, so
    the send that follows fails. That is a reconnect, not an error worth a
    stack trace - and there is nothing to retry on a dead socket."""
    eng = HueSyncEngine("127.0.0.1", app.port, mode="video", intensity="")
    orig = eng._reconcile
    calls = {"n": 0}

    def flaky(ws):
        calls["n"] += 1
        if calls["n"] == 1:
            raise hs.WebSocketClosed("not connected")
        orig(ws)

    eng._reconcile = flaky
    try:
        eng.start()
        assert wait(lambda: eng.state().syncing, 8.0)
        assert app.connections >= 2 and eng.alive()
    finally:
        eng.close()
        app.close()


def test_run_survives_a_session_crash_and_reconnects(app):
    eng = HueSyncEngine("127.0.0.1", app.port, mode="video", intensity="")
    orig = eng._session
    calls = {"n": 0}

    def flaky(ws):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("session boom")
        orig(ws)

    eng._session = flaky
    try:
        eng.start()
        assert wait(lambda: eng.state().syncing, 8.0)
        assert app.connections >= 2 and eng.alive()
    finally:
        eng.close()
        app.close()


def test_area_switch_crash_is_surfaced_then_retried(app):
    from tests.test_areas import SwitchableEngine

    eng = SwitchableEngine(app, area="area-office")
    try:
        assert wait(lambda: eng.state().connected)
        eng.areas = lambda: 1 / 0                        # crash inside the switch
        eng.start()
        eng.set_area("area-living")
        assert wait(lambda: eng.state().error, 8.0)      # failure surfaced...
        assert eng.alive()                               # ...thread still with us
        del eng.areas                                    # heal the fault
        eng._restart_failed_at = float("-inf")           # skip the 60 s retry guard
        assert wait(lambda: eng.state().syncing and eng.state().area_id == "area-living", 10.0)
    finally:
        eng.close()
        app.close()


def test_pause_stop_due():
    assert not pause_stop_due(None, 100.0, 900.0, False)     # not paused
    assert not pause_stop_due(70.0, 100.0, 900.0, False)     # 30 s < 15 min
    assert pause_stop_due(70.0, 100.0, 30.0, False)          # 30 s >= 30 s
    assert pause_stop_due(70.0, 100.0, 15.0, False)          # the music default
    assert not pause_stop_due(70.0, 100.0, 30.0, True)       # already stopped
    assert not pause_stop_due(70.0, 100.0, 0.0, False)       # feature off
    assert not pause_stop_due(70.0, 100.0, -1.0, False)      # negative = off
