"""Small custom widgets: animated toggle, colourful segmented control, cards,
state pill, drift gauge, sparkline, background worker."""
from __future__ import annotations

from collections import deque
from typing import Callable

from PySide6.QtCore import (Property, QEasingCurve, QObject, QPropertyAnimation, QRectF, QRunnable,
                            Qt, QThreadPool, Signal, Slot)
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from . import theme


# -- background work ---------------------------------------------------------------------
class _Dispatcher(QObject):
    """Lives in the GUI thread; workers emit into it so callbacks run on the GUI thread."""
    call = Signal(object, object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._invoke, Qt.QueuedConnection)

    @Slot(object, object)
    def _invoke(self, fn, arg):
        try:
            fn(arg)
        except Exception:
            import logging
            logging.getLogger("hue-ghost.gui").exception("callback failed")


_dispatcher: _Dispatcher | None = None


def _dispatch() -> _Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = _Dispatcher()     # first use is from the GUI thread
    return _dispatcher


class Worker(QRunnable):
    def __init__(self, fn: Callable, on_done: Callable | None = None, on_error: Callable | None = None):
        super().__init__()
        self.fn, self.on_done, self.on_error = fn, on_done, on_error

    def run(self) -> None:
        try:
            res = self.fn()
        except Exception as e:  # surfaced to the UI
            if self.on_error:
                _dispatch().call.emit(self.on_error, str(e))
            return
        if self.on_done:
            _dispatch().call.emit(self.on_done, res)


def run_async(fn: Callable, on_done: Callable | None = None, on_error: Callable | None = None) -> None:
    _dispatch()
    QThreadPool.globalInstance().start(Worker(fn, on_done, on_error))


# -- toggle switch ---------------------------------------------------------------------------
class ToggleSwitch(QAbstractButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(52, 28)
        self._pos = 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)   # not "pos": QWidget already has one
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    def _get_pos(self) -> float:
        return self._pos

    def _set_pos(self, v: float) -> None:
        self._pos = v
        self.update()

    knob = Property(float, _get_pos, _set_pos)

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def setChecked(self, on: bool) -> None:  # noqa: N802 (Qt API)
        super().setChecked(on)
        self._pos = 1.0 if on else 0.0
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        off, on = QColor("#3a4151"), QColor(theme.ACCENT)
        t = self._pos
        bg = QColor(int(off.red() + (on.red() - off.red()) * t), int(off.green() + (on.green() - off.green()) * t),
                    int(off.blue() + (on.blue() - off.blue()) * t))
        if not self.isEnabled():
            bg.setAlpha(120)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        r = h - 6
        x = 3 + (w - h) * t
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(x, 3, r, r))


# -- segmented control ---------------------------------------------------------------------
class Segmented(QWidget):
    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], colors: dict[str, str] | None = None, parent=None):
        """options: [(value, label)]; colors: value -> hex for the checked state."""
        super().__init__(parent)
        self._colors = colors or {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        for value, label in options:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self._group.addButton(b)
            self._buttons[value] = b
            lay.addWidget(b)
            b.toggled.connect(lambda on, v=value: self._on_toggle(v, on))
        self._restyle()

    def _on_toggle(self, value: str, on: bool) -> None:
        self._restyle()
        if on:
            self.changed.emit(value)

    def _restyle(self) -> None:
        for value, b in self._buttons.items():
            color = self._colors.get(value, theme.ACCENT)
            if b.isChecked():
                b.setStyleSheet("QPushButton{background:%s;border:none;color:white;}" % color)
            else:
                b.setStyleSheet("QPushButton{background:%s;border:1px solid %s;color:%s;}"
                                % (theme.CARD_HI, theme.BORDER, theme.MUTED))

    def value(self) -> str | None:
        for v, b in self._buttons.items():
            if b.isChecked():
                return v
        return None

    def set_value(self, value: str | None) -> None:
        b = self._buttons.get(value or "")
        if b is not None and not b.isChecked():
            b.blockSignals(True)
            b.setChecked(True)
            b.blockSignals(False)
            self._restyle()


# -- layout helpers ------------------------------------------------------------------------
class Card(QFrame):
    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 16)
        self.body.setSpacing(8)
        if title:
            t = QLabel(title.upper())
            t.setObjectName("cardTitle")
            self.body.addWidget(t)

    def add(self, w: QWidget) -> QWidget:
        self.body.addWidget(w)
        return w

    def add_row(self, *widgets: QWidget, stretch_last: bool = False) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        for w in widgets:
            row.addWidget(w)
        if stretch_last:
            row.addStretch(1)
        self.body.addLayout(row)
        return row


def label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    lb.setWordWrap(wrap)
    return lb


def pill(text: str, color: str) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("pill")
    lb.setStyleSheet("QLabel#pill{background:%s;}" % color)
    lb.setAlignment(Qt.AlignCenter)
    return lb


def set_pill(lb: QLabel, text: str, color: str) -> None:
    lb.setText(text)
    lb.setStyleSheet("QLabel#pill{background:%s;}" % color)


def button(text: str, kind: str | None = None, on_click: Callable | None = None) -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    b.setCursor(Qt.PointingHandCursor)
    if on_click:
        b.clicked.connect(on_click)
    return b


class Banner(QFrame):
    def __init__(self, text: str, kind: str = "warn", parent=None):
        super().__init__(parent)
        self.setObjectName("banner" if kind == "warn" else "bannerInfo")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        self.text = QLabel(text)
        self.text.setWordWrap(True)
        lay.addWidget(self.text, 1)
        self.actions = QHBoxLayout()
        lay.addLayout(self.actions)


# -- gauges ------------------------------------------------------------------------------------
class DriftBar(QWidget):
    """Where the ghost is relative to where it should be: -1 s .. +1 s."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(44)
        self.value: float | None = None

    def set_value(self, v: float | None) -> None:
        self.value = v
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        track_y = h / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.BORDER))
        p.drawRoundedRect(QRectF(10, track_y - 4, w - 20, 8), 4, 4)
        # green centre zone (+/- 0.15 s), amber to 0.5, red beyond
        cx = w / 2
        scale = (w - 20) / 2.0
        for lo, hi, col in ((0.0, 0.15, theme.GOOD), (0.15, 0.5, theme.WARN), (0.5, 1.0, theme.BAD)):
            c = QColor(col)
            c.setAlpha(70)
            p.setBrush(c)
            p.drawRect(QRectF(cx - hi * scale, track_y - 4, (hi - lo) * scale, 8))
            p.drawRect(QRectF(cx + lo * scale, track_y - 4, (hi - lo) * scale, 8))
        p.setPen(QPen(QColor(theme.MUTED), 1))
        p.drawLine(int(cx), int(track_y - 10), int(cx), int(track_y + 10))
        f = QFont()
        f.setPointSize(8)
        p.setFont(f)
        p.drawText(QRectF(4, h - 14, 60, 12), Qt.AlignLeft, "-1 s behind")
        p.drawText(QRectF(w - 64, h - 14, 60, 12), Qt.AlignRight, "+1 s ahead")
        if self.value is None:
            return
        v = max(-1.0, min(1.0, self.value))
        x = cx + v * scale
        col = theme.GOOD if abs(self.value) <= 0.15 else theme.WARN if abs(self.value) <= 0.5 else theme.BAD
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(col))
        p.drawEllipse(QRectF(x - 8, track_y - 8, 16, 16))


class Sparkline(QWidget):
    def __init__(self, n: int = 120, parent=None):
        super().__init__(parent)
        self.values: deque[float] = deque(maxlen=n)
        self.setMinimumHeight(56)

    def push(self, v: float | None) -> None:
        if v is not None:
            self.values.append(v)
            self.update()

    def clear(self) -> None:
        self.values.clear()
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        mid = h / 2
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(0, int(mid), w, int(mid))
        if len(self.values) < 2:
            return
        n = self.values.maxlen or 1
        step = w / max(1, n - 1)
        path = QPainterPath()
        for i, v in enumerate(self.values):
            y = mid - max(-1.0, min(1.0, v)) * (h / 2 - 4)
            x = i * step
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        grad = QLinearGradient(0, 0, w, 0)
        grad.setColorAt(0, QColor(theme.ACCENT))
        grad.setColorAt(1, QColor(theme.ACCENT2))
        p.setPen(QPen(grad, 2))
        p.drawPath(path)
