"""The window adapts to its width: rows wrap rather than widening the page.

Needs PySide6 (the ``gui`` extra); skipped where it is not installed."""
import os
import sys

import pytest

pytest.importorskip("PySide6")
if sys.platform != "win32":
    # widths are measured in real fonts on Windows, the platform the app is
    # for; elsewhere there may be no display at all
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton, QScrollArea, QWidget  # noqa: E402

from hueghost.config import Config  # noqa: E402

# the narrowest content area: the minimum window less the navigation rail
NARROW = 780 - 222 - 20


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _Api:
    def __getattr__(self, k):
        return lambda *a, **kw: {}


class _Ctx:
    daemon = _Api()
    api = _Api()

    def toast(self, *a):
        pass


def _inner(page):
    return page.findChild(QScrollArea).widget()


def test_flow_layout_wraps_instead_of_widening(app):
    from hueghost.gui.widgets import FlowLayout
    w = QWidget()
    f = FlowLayout(w)
    buttons = [QPushButton("button %d" % i) for i in range(8)]
    for b in buttons:
        f.addWidget(b)
    widest = max(b.sizeHint().width() for b in buttons)
    assert f.minimumSize().width() == widest
    one_line = f.heightForWidth(10_000)
    assert f.heightForWidth(widest) > one_line


def test_the_sources_page_fits_the_smallest_window(app):
    from hueghost.gui import pages
    cfg = Config({"sources": [
        {"source": "pc", "name": "Video players", "mode": "video", "detect": "either",
         "exe": "vlc.exe,mpc-hc64.exe,mpc-hc.exe,mpc-be64.exe,potplayermini64.exe,kodi.exe,plex.exe,"
                "jellyfinmediaplayer.exe", "area_id": "a1"},
        {"source": "pc", "name": "Rockstar Games Launcher Redirector", "exe": "playgtav.exe", "mode": "games"},
        {"source": "jellyfin", "device_name_contains": "Apple TV in the living room", "use_audio": True}]})
    page = pages.PlayerPage(_Ctx())
    page._areas = [{"id": "a1", "name": "A very long entertainment area name indeed"}]
    for b in cfg.bindings():
        page._append_row(b)
    assert _inner(page).minimumSizeHint().width() <= NARROW


def test_every_page_fits_the_smallest_window(app):
    from hueghost.gui import pages
    for cls in (pages.HomePage, pages.SyncPage, pages.DisplayPage, pages.HueSyncPage,
                pages.HomeAssistantPage, pages.SettingsPage, pages.LogPage):
        assert _inner(cls(_Ctx())).minimumSizeHint().width() <= NARROW, cls.__name__


def test_a_row_keeps_react_to_audio(app):
    from hueghost.gui.pages import PlayerRow
    for v in (None, True, False):
        row = PlayerRow({"source": "pc", "exe": "game.exe", "mode": "games", "use_audio": v}, [],
                        lambda r: None, lambda r, d: None)
        assert row.value()["use_audio"] is v
    music = PlayerRow({"source": "pc", "exe": "spotify.exe", "mode": "music"}, [], lambda r: None, lambda r, d: None)
    assert not music.audio.isEnabled()          # music mode always listens
