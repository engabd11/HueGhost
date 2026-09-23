"""Hue Ghost desktop app: the daemon runs inside, the window is the control panel,
closing it keeps the tray icon alive."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading

from PySide6.QtCore import QRectF, QSettings, Qt, QTimer
from PySide6.QtGui import (QAction, QColor, QGuiApplication, QIcon, QLinearGradient,
                           QPainter, QPixmap)
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu, QPushButton,
                               QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

from .. import __author__, __version__
from ..config import Config
from ..daemon import IDLE, Daemon
from ..webapi import WebApi
from . import theme
from .pages import PAGES, Context
from .widgets import StatusPill, ToggleSwitch, icon, icon_css, icon_family, set_pill

log = logging.getLogger("hue-ghost.gui")


def ghost_pixmap(color: str | None, size: int = 64) -> QPixmap:
    """The ghost mark. ``color=None`` paints it in the aurora gradient (brand);
    a colour paints it flat (tray state)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    if color is None:
        grad = QLinearGradient(0, 0, size, size)
        for i, c in enumerate(theme.AURORA):
            grad.setColorAt(i / (len(theme.AURORA) - 1), QColor(c))
        p.setBrush(grad)
    else:
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
    return pm


def ghost_icon(color: str | None, size: int = 64) -> QIcon:
    return QIcon(ghost_pixmap(color, size))


def status_key(st: dict) -> str:
    """One word for the whole app state, used by the header pill and the tray."""
    if not st.get("enabled"):
        return "disabled"
    if st.get("setup_required"):
        return "setup"
    state = st.get("state") or IDLE
    if state in ("ghosting", "syncing") and (st.get("system") or {}).get("locked"):
        return "locked"
    if state == IDLE and not (st.get("jellyfin") or {}).get("ok"):
        return "error"
    return state


class NavButton(QPushButton):
    """Nav item. The icon and text are child widgets in a layout, so all
    spacing lives in the layout (not in stylesheet margin/padding, which the
    painted highlight would honour but the children would not)."""

    def __init__(self, glyph: str, text: str):
        super().__init__()
        self.setObjectName("navBtn")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(40)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(12)
        self.ic = icon(glyph, 15)
        self.ic.setObjectName("navIcon")
        self.ic.setFixedSize(20, 40)
        self.ic.setAlignment(Qt.AlignCenter)
        self.ic.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay.addWidget(self.ic, 0, Qt.AlignVCenter)
        lb = QLabel(text)
        lb.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lb.setAttribute(Qt.WA_TransparentForMouseEvents)
        lb.setStyleSheet("background:transparent;")
        lay.addWidget(lb, 1, Qt.AlignVCenter)
        self.lb = lb
        self.toggled.connect(self._restyle)
        self._restyle(False)

    def _restyle(self, on: bool) -> None:
        self.ic.setProperty("active", "true" if on else "false")
        self.ic.setStyleSheet(icon_css(15, theme.ACCENT if on else theme.MUTED))
        self.lb.setStyleSheet("background:transparent;color:%s;font-weight:%s;"
                              % (theme.TEXT if on else theme.TEXT_2, 600 if on else 400))


class MainWindow(QMainWindow):
    def __init__(self, daemon: Daemon):
        super().__init__()
        self.daemon = daemon
        self.api = WebApi(daemon)
        self.setWindowTitle("Hue Ghost")
        self.setMinimumSize(960, 660)
        self.setWindowIcon(ghost_icon(None))
        self._restore_geometry()
        self.last_status: dict = {}

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # -- nav rail
        nav = QFrame()
        nav.setObjectName("nav")
        nav.setFixedWidth(222)
        nv = QVBoxLayout(nav)
        nv.setContentsMargins(0, 18, 0, 14)
        nv.setSpacing(0)
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(18, 0, 14, 0)
        brand_row.setSpacing(10)
        mark = QLabel()
        mark.setObjectName("navMark")
        mark.setPixmap(ghost_pixmap(None, 30))
        mark.setFixedSize(30, 30)
        brand_row.addWidget(mark, 0, Qt.AlignVCenter)
        bcol = QVBoxLayout()
        bcol.setSpacing(0)
        brand = QLabel("Hue Ghost")
        brand.setObjectName("brand")
        sub = QLabel("software Sync Box for Jellyfin")
        sub.setObjectName("brandSub")
        bcol.addWidget(brand)
        bcol.addWidget(sub)
        brand_row.addLayout(bcol, 1)
        nv.addLayout(brand_row)
        nv.addSpacing(18)
        # the buttons' own box is the highlight: inset the column, not the button
        self.nav_col = QVBoxLayout()
        self.nav_col.setContentsMargins(10, 0, 10, 0)
        self.nav_col.setSpacing(2)
        nv.addLayout(self.nav_col)
        self.nav_buttons: dict[str, NavButton] = {}
        h.addWidget(nav)

        # -- content column
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(28, 20, 28, 8)
        self.page_title = QLabel("")
        self.page_title.setObjectName("pageTitle")
        self.page_sub = QLabel("")
        self.page_sub.setObjectName("pageSub")
        tcol = QVBoxLayout()
        tcol.setSpacing(2)
        tcol.addWidget(self.page_title)
        tcol.addWidget(self.page_sub)
        hl.addLayout(tcol, 1)
        self.state_pill = StatusPill("starting", theme.STATE_COLORS["idle"])
        hl.addWidget(self.state_pill, 0, Qt.AlignVCenter)
        hl.addSpacing(16)
        sync_lbl = QLabel("Sync")
        sync_lbl.setObjectName("headerLabel")
        hl.addWidget(sync_lbl)
        hl.addSpacing(6)
        self.enable = ToggleSwitch()
        self.enable.setToolTip("Master switch: follow the TV and drive the lights")
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
            b = NavButton(cls.key, cls.title)
            b.clicked.connect(lambda _=False, k=cls.key: self.goto(k))
            self.nav_buttons[cls.key] = b
            self.nav_col.addWidget(b)
        nv.addStretch(1)
        foot = QVBoxLayout()
        foot.setContentsMargins(18, 0, 14, 0)
        foot.setSpacing(1)
        by = QLabel("built by")
        by.setObjectName("brandFoot")
        who = QLabel(__author__)
        who.setObjectName("brandFootStrong")
        ver = QLabel("v" + __version__)
        ver.setObjectName("brandFoot")
        foot.addWidget(by)
        foot.addWidget(who)
        foot.addSpacing(6)
        foot.addWidget(ver)
        nv.addLayout(foot)

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
        if not icon_family():
            log.info("no Segoe icon font found; using text fallbacks")
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
        self.toast_lbl.setStyleSheet("QLabel#pill{background:%s;color:%s;padding:8px 16px;border-radius:14px;}"
                                     % (color, theme.BG if kind == "ok" else "white"))
        self.toast_lbl.adjustSize()
        self.toast_lbl.move((self.width() - self.toast_lbl.width()) // 2, self.height() - 56)
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
        key = status_key(st)
        self.state_pill.set(theme.STATE_LABELS.get(key, key), theme.STATE_COLORS.get(key, theme.STATE_COLORS["idle"]),
                            pulsing=(key == "syncing"))
        if self.enable.isChecked() != st["enabled"]:
            self.enable.setChecked(st["enabled"])
        page = self.stack.currentWidget()
        if page is not None:
            try:
                page.refresh(st)
            except Exception:
                log.exception("refresh failed")

    # -- window geometry ------------------------------------------------------
    def _settings(self) -> QSettings:
        return QSettings("Cyborg Automation AU", "Hue Ghost")

    def _restore_geometry(self) -> None:
        """Open big enough for the content, where it was left last time.

        The default used to be 1080x720, which cut the Home grid off - every
        launch started with a resize. Both the default and a remembered size
        are clamped to the screen, so a smaller display still gets a window
        that fits on it."""
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        saved = self._settings().value("geometry")
        if saved is not None and self.restoreGeometry(saved):
            if avail is None or avail.intersects(self.frameGeometry()):
                return                    # still on a screen that exists
        w, h = 1260, 840
        if avail is not None:
            w = max(self.minimumWidth(), min(w, avail.width() - 80))
            h = max(self.minimumHeight(), min(h, avail.height() - 80))
        self.resize(w, h)
        if avail is not None:
            self.move(avail.center() - self.rect().center())

    def _save_geometry(self) -> None:
        try:
            self._settings().setValue("geometry", self.saveGeometry())
        except Exception:
            log.debug("could not save the window geometry", exc_info=True)

    def hideEvent(self, e) -> None:  # noqa: N802
        self._save_geometry()
        super().hideEvent(e)

    def closeEvent(self, e) -> None:  # noqa: N802
        # keep running in the tray
        self._save_geometry()
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
        key = status_key(st)
        if key != self._last_key:
            self.setIcon(ghost_icon(theme.STATE_COLORS.get(key, theme.STATE_COLORS["idle"])))
            self._last_key = key
        f = st.get("follow") or {}
        tip = "Hue Ghost — %s" % theme.STATE_LABELS.get(key, key)
        if f.get("playing") and f.get("item"):
            tip += "\n%s" % f["item"]
            if st.get("drift_s") is not None:
                tip += "\ndrift %+.2f s" % st["drift_s"]
        self.setToolTip(tip[:127])
        self.a_enabled.setChecked(st["enabled"])


def run_gui(cfg: Config, minimized: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Hue Ghost")
    app.setOrganizationName(__author__)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(theme.QSS)
    app.setWindowIcon(ghost_icon(None))

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
