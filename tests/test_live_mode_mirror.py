"""What the Home controls show: the Hue Sync app, at all times.

Every source carries its own mode and intensity now, so the saved settings are
only the fallback for sources that have not chosen one. Reading them left the
controls on whatever the previous session happened to leave behind - the app
would report ``video`` / ``subtle`` while the buttons still said ``music`` /
``high``. They follow the app instead, including across the restart Hue Ghost
performs itself to apply an entertainment area.
"""
import time

import pytest

from hueghost.config import Config
from hueghost.daemon import Daemon
from hueghost.engines import huesync as hs
from tests.mock_huesync import MockHueSync


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(hs, "BACKOFF_MIN_S", 0.1)
    monkeypatch.setattr(hs, "BACKOFF_MAX_S", 0.3)


def wait(pred, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.fixture
def daemon(tmp_path):
    app = MockHueSync(mode="video", intensity="subtle")
    cfg = Config({
        "jellyfin": {"url": "http://jf", "api_key": "k", "follow": {"device_id": "atv"}},
        # a stale default: what an earlier session adopted, not what anyone picked
        "engine": {"type": "huesync", "huesync": {"host": "127.0.0.1", "port": app.port,
                                                  "mode": "music", "intensity": "high"}},
        "control": {"port": 0},
    }, str(tmp_path / "config.json"))
    d = Daemon(cfg)
    yield d, app
    d.engine.close()
    app.close()


def test_the_controls_show_what_the_app_reports_not_the_saved_default(daemon):
    d, app = daemon
    assert wait(lambda: d.engine.state().mode == "video")
    st = d.status()
    assert (st["mode"], st["intensity"]) == ("video", "subtle")
    assert (st["mode_default"], st["intensity_default"]) == ("music", "high")


def test_they_keep_following_it_when_the_app_restarts(daemon):
    """Applying an entertainment area kills Hue Sync and launches it again.
    While the socket is down there is nothing new to report, so the last thing
    the app said stands - and the first update after it is back wins."""
    d, app = daemon
    assert wait(lambda: d.status()["mode"] == "video")

    app.mode, app.intensity = "games", "extreme"     # the user picks this in the app
    app.drop_clients()                               # ... and it restarts
    assert d.status()["mode"] == "video", "hold the last known value, never the stale default"

    assert wait(lambda: d.engine.state().connected and d.engine.state().mode == "games", 8.0)
    st = d.status()
    assert (st["mode"], st["intensity"]) == ("games", "extreme")
    assert st["mode_default"] == "music", "the saved default is untouched by any of this"


def test_a_mode_hue_ghost_has_no_button_for_falls_back_to_the_default(daemon):
    """Hue Sync's "scenes" syncs nothing and has no control here."""
    d, app = daemon
    assert wait(lambda: d.status()["mode"] == "video")
    app.mode = "scenes"
    app.broadcast_state()
    assert wait(lambda: d.engine.state().mode == "scenes")
    assert d.status()["mode"] == "music", "nothing of ours to show -> the default"
