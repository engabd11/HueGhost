"""Hue Ghost desktop app: the daemon runs inside, the window is the control panel,
closing it keeps the tray icon alive."""
from __future__ import annotations

import logging
import subprocess
import sys
import threading

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu, QPushButton,
                               QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

from .. import __version__
from ..config import Config
from ..daemon import IDLE, Daemon
from ..webapi import WebApi
from . import theme
from .pages import NAV_ICONS, PAGES, Context
from .widgets import ToggleSwitch, pill, set_pill

log = logging.getLogger("hue-ghost.gui")


def ghost_icon(color: str, size: int = 64) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    r = QRectF(size * 0.12, size * 0.08, size * 0.76, size * 0.84)
    p.drawRoundedRect(r, size * 0.38, size * 0.38)
    # skirt: three bumps
    p.setBrush(QColor(theme.BG))
    for i in range(3):
        x = size * (0.12 + i * 0.253)
        p.drawEllipse(QRectF(x, size * 0.80, size * 0.253, size * 0.24))
    p.setBrush(QColor("white"))
    e = size * 0.11
    p.drawEllipse(QRectF(size * 0.36 - e / 2, size * 0.40 - e / 2, e, e))
    p.drawEllipse(QRectF(size * 0.64 - e / 2, size * 0.40 - e / 2, e, e))
    p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self, daemon: Daemon):
        super().__init__()
        self.daemon = daemon
        self.api = WebApi(daemon)
        self.setWindowTitle("Hue Ghost")
        self.resize(1040, 700)
        self.setMinimumSize(880, 600)
        self.setWindowIcon(ghost_icon(theme.ACCENT))

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # nav rail
        nav = QFrame()
        nav.setObjectName("nav")
        nav.setFixedWidth(210)
        nv = QVBoxLayout(nav)
        nv.setContentsMargins(0, 0, 0, 12)
        nv.setSpacing(0)
        brand = QLabel("Hue Ghost")
        brand.setObjectName("brand")
        sub = QLabel("software Sync Box for Jellyfin")
        sub.setObjectName("brandSub")
        nv.addWidget(brand)
        nv.addWidget(sub)
        self.nav_buttons: dict[str, QPushButton] = {}
        h.addWidget(nav)

        # content column
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(24, 18, 24, 6)
        self.page_title = QLabel("")
        self.page_title.setObjectName("pageTitle")
        self.page_sub = QLabel("")
        self.page_sub.setObjectName("pageSub")
        tcol = QVBoxLayout()
        tcol.setSpacing(2)
        tcol.addWidget(self.page_title)
        tcol.addWidget(self.page_sub)
        hl.addLayout(tcol, 1)
        self.state_pill = pill("starting", theme.STATE_COLORS["idle"])
        self.state_pill.setFixedHeight(26)
        hl.addWidget(self.state_pill, 0, Qt.AlignVCenter)
        hl.addSpacing(12)
        hl.addWidget(QLabel("Sync"))
        self.enable = ToggleSwitch()
        self.enable.setChecked(daemon.enabled)
        self.enable.clicked.connect(lambda on: daemon.action("on" if on else "off", {}))
        hl.addWidget(self.enable)
        col.addWidget(header)
        self.stack = QStackedWidget()
        col.addWidget(self.stack, 1)
        h.addLayout(col, 1)

        ctx = Context(daemon=daemon, api=self.api, toast=self.toast, goto=self.goto)
        self.pages = {}
        for cls in PAGES:
            page = cls(ctx)
            self.pages[cls.key] = page
            self.stack.addWidget(page)
            b = QPushButton("%s   %s" % (NAV_ICONS.get(cls.key, ""), cls.title))
            b.setObjectName("navBtn")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=cls.key: self.goto(k))
            self.nav_buttons[cls.key] = b
            nv.addWidget(b)
        nv.addStretch(1)
        ver = QLabel("v" + __version__)
        ver.setObjectName("brandSub")
        nv.addWidget(ver)

        # toast
        self.toast_lbl = QLabel(self)
        self.toast_lbl.setObjectName("pill")
        self.toast_lbl.setAlignment(Qt.AlignCenter)
        self.toast_lbl.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.toast_lbl.hide)

        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        import os
        self.goto(os.environ.get("HUEGHOST_PAGE") or ("player" if daemon.cfg.problems() else "home"))

    # -- navigation ---------------------------------------------------------------------
    def goto(self, key: str) -> None:
        page = self.pages[key]
        for k, b in self.nav_buttons.items():
            b.setChecked(k == key)
        self.stack.setCurrentWidget(page)
        self.page_title.setText(page.title)
        self.page_sub.setText(page.subtitle)
        try:
            page.on_show()
        except Exception as e:
            log.exception("page load failed")
            self.toast(str(e), "bad")

    def toast(self, msg: str, kind: str = "ok") -> None:
        color = {"ok": theme.GOOD, "warn": theme.WARN, "bad": theme.BAD}.get(kind, theme.INFO)
        set_pill(self.toast_lbl, msg, color)
        self.toast_lbl.adjustSize()
        self.toast_lbl.move((self.width() - self.toast_lbl.width()) // 2, self.height() - 48)
        self.toast_lbl.show()
        self.toast_lbl.raise_()
        self._toast_timer.start(3500)

    # -- live refresh ---------------------------------------------------------------------
    def _tick(self) -> None:
        try:
            st = self.daemon.status()
        except Exception:
            return
        self.last_status = st
        key = "disabled" if not st["enabled"] else "setup" if st.get("setup_required") else st["state"]
        if key == IDLE and not st.get("jellyfin", {}).get("ok"):
            key = "error"
        labels = {"disabled": "disabled", "setup": "setup needed", "idle": "idle", "ghosting": "ghost playing",
                  "syncing": "syncing", "error": "Jellyfin unreachable"}
        set_pill(self.state_pill, labels.get(key, key), theme.STATE_COLORS.get(key, theme.STATE_COLORS["idle"]))
        if self.enable.isChecked() != st["enabled"]:
            self.enable.setChecked(st["enabled"])
        page = self.stack.currentWidget()
        if page is not None:
            try:
                page.refresh(st)
            except Exception:
                log.exception("refresh failed")

    def closeEvent(self, e) -> None:  # noqa: N802
        # keep running in the tray
        e.ignore()
        self.hide()


class Tray(QSystemTrayIcon):
    def __init__(self, win: MainWindow, daemon: Daemon, app: QApplication):
        super().__init__(ghost_icon(theme.STATE_COLORS["idle"]))
        self.win, self.daemon, self.app = win, daemon, app
        menu = QMenu()
        a_open = QAction("Open Hue Ghost", menu)
        a_open.triggered.connect(self.show_window)
        menu.addAction(a_open)
        self.a_enabled = QAction("Sync enabled", menu)
        self.a_enabled.setCheckable(True)
        self.a_enabled.setChecked(daemon.enabled)
        self.a_enabled.triggered.connect(lambda on: daemon.action("on" if on else "off", {}))
        menu.addAction(self.a_enabled)
        menu.addSeparator()
        a_quit = QAction("Quit", menu)
        a_quit.triggered.connect(self.quit)
        menu.addAction(a_quit)
        self.setContextMenu(menu)
        self.activated.connect(lambda reason: self.show_window() if reason == QSystemTrayIcon.Trigger else None)
        self.setToolTip("Hue Ghost")
        self._last_key = None
        t = QTimer(self)
        t.setInterval(1500)
        t.timeout.connect(self._tick)
        t.start()
        self._timer = t

    def show_window(self) -> None:
        self.win.show()
        self.win.raise_()
        self.win.activateWindow()

    def quit(self) -> None:
        self.hide()
        self.app.quit()

    def _tick(self) -> None:
        try:
            st = self.daemon.status()
        except Exception:
            return
        key = "disabled" if not st["enabled"] else "setup" if st.get("setup_required") else st["state"]
        if key != self._last_key:
            self.setIcon(ghost_icon(theme.STATE_COLORS.get(key, theme.STATE_COLORS["idle"])))
            self._last_key = key
        f = st.get("follow") or {}
        tip = "Hue Ghost: %s" % key
        if f.get("playing"):
            tip += "\n%s\ndrift %s s" % (f.get("item"), st.get("drift_s"))
        self.setToolTip(tip[:127])
        self.a_enabled.setChecked(st["enabled"])


def run_gui(cfg: Config, minimized: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Hue Ghost")
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(theme.QSS)
    app.setWindowIcon(ghost_icon(theme.ACCENT))

    daemon = Daemon(cfg)
    t = threading.Thread(target=daemon.run, name="hue-ghost-daemon", daemon=True)
    t.start()

    win = MainWindow(daemon)
    tray = Tray(win, daemon, app)
    tray.show()
    if not minimized or cfg.problems():
        win.show()
    else:
        tray.showMessage("Hue Ghost", "Running in the tray - lights follow the TV automatically.",
                         QSystemTrayIcon.Information, 4000)

    # stop the app when the daemon asks for a restart
    def watch():
        if daemon.restart_requested or not t.is_alive():
            app.quit()
    wt = QTimer()
    wt.setInterval(500)
    wt.timeout.connect(watch)
    wt.start()

    rc = app.exec()
    daemon.stop()
    t.join(timeout=5)
    if daemon.restart_requested:
        from ..winutil import tray_command
        cmd = tray_command()
        cmd = [c for c in cmd if c != "--minimized"]
        log.info("relaunching %s", cmd)
        subprocess.Popen(cmd, close_fds=True)
    return rc
