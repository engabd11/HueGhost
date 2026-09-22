"""Small custom widgets: Fluent icon glyphs, animated toggle, colourful
segmented control, cards + aligned form rows, chips, status pill, poster,
drift gauge, sparkline, background worker."""
from __future__ import annotations

from collections import deque
from typing import Callable

from PySide6.QtCore import (Property, QEasingCurve, QObject, QPropertyAnimation, QRectF, QRunnable, QTimer,
                            Qt, QThreadPool, Signal, Slot)
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap)
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


# -- icons (Segoe Fluent Icons / MDL2 glyphs, with a text fallback) --------------------------------
GLYPHS = {
    "home": "", "sync": "", "player": "", "display": "", "huesync": "",
    "ha": "", "settings": "", "log": "", "check": "", "warn": "",
    "info": "", "refresh": "", "copy": "", "open": "", "play": "",
    "pause": "", "power": "", "bulb": "", "clock": "", "up": "",
    "down": "", "remove": "", "add": "", "tv": "", "folder": "",
    "search": "", "lock": "", "moon": "", "link": "", "heart": "",
}
FALLBACK = {"up": "▲", "down": "▼", "remove": "✕", "add": "+", "check": "✓"}
_icon_family: str | None = None


def icon_family() -> str | None:
    global _icon_family
    if _icon_family is None:
        fams = set(QFontDatabase.families())
        _icon_family = next((f for f in theme.ICON_FONTS if f in fams), "")
    return _icon_family or None


def icon_font(size: int = 14) -> QFont | None:
    fam = icon_family()
    if not fam:
        return None
    f = QFont(fam)
    f.setPixelSize(size)
    return f


def icon_css(size: int, color: str | None = None) -> str:
    """Per-widget stylesheet for a glyph label: the app-wide ``*`` font rule
    would otherwise override the font set with setFont()."""
    fam = icon_family()
    css = "background:transparent;font-size:%dpx;" % size
    if fam:
        css += "font-family:'%s';" % fam
    if color:
        css += "color:%s;" % color
    return css


def icon(name: str, size: int = 14, color: str | None = None, parent=None) -> QLabel:
    """A label showing one icon glyph (or a plain-text stand-in)."""
    lb = QLabel(parent)
    f = icon_font(size)
    if f is not None:
        lb.setFont(f)
        lb.setText(GLYPHS.get(name, ""))
    else:
        lb.setText(FALLBACK.get(name, ""))
    lb.setAlignment(Qt.AlignCenter)
    lb.setStyleSheet(icon_css(size, color))
    return lb


def icon_text(name: str) -> str:
    """Glyph string for use inside a QPushButton (needs the icon font set on it)."""
    return GLYPHS.get(name, FALLBACK.get(name, "")) if icon_family() else FALLBACK.get(name, "")


def icon_button(name: str, tip: str = "", on_click: Callable | None = None, kind: str = "icon") -> QPushButton:
    b = QPushButton(icon_text(name))
    f = icon_font(13)
    if f is not None:
        b.setFont(f)
    b.setObjectName(kind)
    b.setCursor(Qt.PointingHandCursor)
    if tip:
        b.setToolTip(tip)
    if on_click:
        b.clicked.connect(on_click)
    return b


# -- toggle switch ---------------------------------------------------------------------------
class ToggleSwitch(QAbstractButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(46, 26)
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
        off, on = QColor(theme.BORDER_HI), QColor(theme.ACCENT)
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
        p.setBrush(QColor(theme.BG if t > 0.5 else theme.TEXT))
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
        for value, label_ in options:
            b = QPushButton(label_)
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
            fg = theme.BG if color == theme.ACCENT else "white"
            if b.isChecked():
                b.setStyleSheet("QPushButton{background:%s;border:1px solid %s;color:%s;}" % (color, color, fg))
            else:
                b.setStyleSheet("QPushButton{background:%s;border:1px solid %s;color:%s;}"
                                "QPushButton:hover{background:%s;color:%s;}"
                                % (theme.PANEL, theme.BORDER, theme.MUTED, theme.CARD_HI, theme.TEXT))

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
    def __init__(self, title: str = "", subtitle: str = "", action: QWidget | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 18)
        self.body.setSpacing(10)
        if title or action is not None:
            head = QHBoxLayout()
            head.setSpacing(8)
            tcol = QVBoxLayout()
            tcol.setSpacing(2)
            if title:
                t = QLabel(title.upper())
                t.setObjectName("cardTitle")
                tcol.addWidget(t)
            if subtitle:
                s = QLabel(subtitle)
                s.setObjectName("cardSub")
                s.setWordWrap(True)
                tcol.addWidget(s)
            head.addLayout(tcol, 1)
            if action is not None:
                head.addWidget(action, 0, Qt.AlignTop)
            self.body.addLayout(head)

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

    def form(self, label_text: str, widget: QWidget, hint: str = "", trailing: QWidget | None = None) -> QWidget:
        """One aligned settings row: label column | control (| trailing)."""
        row = FormRow(label_text, widget, hint, trailing)
        self.body.addWidget(row)
        return widget


class FormRow(QWidget):
    LABEL_W = 190

    def __init__(self, label_text: str, widget: QWidget, hint: str = "", trailing: QWidget | None = None,
                 parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        h = QHBoxLayout()
        h.setSpacing(10)
        lb = QLabel(label_text)
        lb.setObjectName("formLabel")
        lb.setFixedWidth(self.LABEL_W)
        lb.setWordWrap(True)
        h.addWidget(lb, 0, Qt.AlignVCenter)
        # a fixed-width control (spin box, toggle) must sit left, next to its
        # label; a free one (line edit, combo) takes the width
        fixed = widget.maximumWidth() < 16777215 or widget.sizePolicy().horizontalPolicy() == QSizePolicy.Fixed
        h.addWidget(widget, 0 if fixed else 1, Qt.AlignVCenter if fixed else Qt.Alignment())
        if trailing is not None:
            h.addWidget(trailing, 0)
        if fixed:
            h.addStretch(1)
        v.addLayout(h)
        if hint:
            hl = QLabel(hint)
            hl.setObjectName("hint")
            hl.setWordWrap(True)
            hl.setContentsMargins(self.LABEL_W + 10, 0, 0, 2)
            v.addWidget(hl)
        self.label = lb


class Divider(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("divider")
        self.setFrameShape(QFrame.NoFrame)


def label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    lb.setWordWrap(wrap)
    return lb


def chip(text: str = "", strong: bool = False) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("chipStrong" if strong else "chip")
    return lb


def pill(text: str, color: str) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("pill")
    lb.setStyleSheet("QLabel#pill{background:%s;}" % color)
    lb.setAlignment(Qt.AlignCenter)
    return lb


def set_pill(lb: QLabel, text: str, color: str) -> None:
    if lb.text() != text:
        lb.setText(text)
    css = "QLabel#pill{background:%s;}" % color
    if lb.styleSheet() != css:
        lb.setStyleSheet(css)


class StatusPill(QWidget):
    """Coloured dot + label; the dot breathes while `pulsing`."""

    def __init__(self, text: str = "", color: str = theme.MUTED, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self._text = text
        self._phase = 0.0
        self._pulsing = False
        self.setFixedHeight(28)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._advance)
        f = QFont()
        f.setPointSizeF(9.5)
        f.setBold(True)
        self.setFont(f)
        self._resize()

    def set(self, text: str, color: str, pulsing: bool = False) -> None:
        changed = text != self._text
        self._text, self._color = text, QColor(color)
        if pulsing != self._pulsing:
            self._pulsing = pulsing
            (self._timer.start if pulsing else self._timer.stop)()
        if changed:
            self._resize()
        self.update()

    def _resize(self) -> None:
        w = self.fontMetrics().horizontalAdvance(self._text) + 40
        self.setFixedWidth(w)

    def _advance(self) -> None:
        self._phase = (self._phase + 0.06) % 1.0
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        bg = QColor(self._color)
        bg.setAlpha(38)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        # dot (+ breathing halo)
        cx, cy = 14, h / 2
        if self._pulsing:
            k = 0.5 + 0.5 * math.sin(self._phase * 2 * math.pi)
            halo = QColor(self._color)
            halo.setAlpha(int(90 * (1 - k)))
            p.setBrush(halo)
            r = 4 + 5 * k
            p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
        p.setBrush(self._color)
        p.drawEllipse(QRectF(cx - 4, cy - 4, 8, 8))
        p.setPen(QPen(self._color.lighter(135)))
        p.drawText(QRectF(26, 0, w - 30, h), Qt.AlignVCenter | Qt.AlignLeft, self._text)


def button(text: str, kind: str | None = None, on_click: Callable | None = None, icon_name: str = "") -> QPushButton:
    b = QPushButton(("%s  %s" % (icon_text(icon_name), text)) if (icon_name and icon_family()) else text)
    if kind:
        b.setObjectName(kind)
    b.setCursor(Qt.PointingHandCursor)
    if on_click:
        b.clicked.connect(on_click)
    return b


class Banner(QFrame):
    def __init__(self, text: str, kind: str = "warn", parent=None):
        super().__init__(parent)
        self.setObjectName({"warn": "banner", "info": "bannerInfo", "bad": "bannerBad"}.get(kind, "banner"))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(10)
        glyph = {"warn": "warn", "info": "info", "bad": "warn"}.get(kind, "info")
        color = {"warn": theme.WARN, "info": theme.INFO, "bad": theme.BAD}.get(kind, theme.INFO)
        lay.addWidget(icon(glyph, 16, color), 0, Qt.AlignTop)
        self.text = QLabel(text)
        self.text.setWordWrap(True)
        lay.addWidget(self.text, 1)
        self.actions = QHBoxLayout()
        lay.addLayout(self.actions)


class Poster(QLabel):
    """Rounded artwork thumbnail that keeps its aspect ratio inside a fixed box."""

    def __init__(self, w: int = 128, h: int = 128, parent=None):
        super().__init__(parent)
        self.setObjectName("poster")
        self.setFixedSize(w, h)
        self.setAlignment(Qt.AlignCenter)
        self._pm: QPixmap | None = None
        self.item_id: str | None = None

    def set_image(self, data: bytes | None, item_id: str | None) -> None:
        self.item_id = item_id
        pm = QPixmap()
        if data and pm.loadFromData(data):
            self._pm = pm
        else:
            self._pm = None
        self.update()

    def paintEvent(self, e) -> None:  # noqa: N802
        super().paintEvent(e)
        if self._pm is None:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            f = icon_font(28)
            if f is not None:
                p.setFont(f)
                p.setPen(QPen(QColor(theme.FAINT)))
                p.drawText(self.rect(), Qt.AlignCenter, GLYPHS["tv"])
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        path = QPainterPath()
        path.addRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), 9, 9)
        p.setClipPath(path)
        pm = self._pm.scaled(self.width() - 2, self.height() - 2, Qt.KeepAspectRatioByExpanding,
                             Qt.SmoothTransformation)
        x = (self.width() - pm.width()) // 2
        y = (self.height() - pm.height()) // 2
        p.drawPixmap(x, y, pm)


# -- gauges ------------------------------------------------------------------------------------
class DriftBar(QWidget):
    """Where the ghost is relative to where it should be: -1 s .. +1 s."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(46)
        self.value: float | None = None

    def set_value(self, v: float | None) -> None:
        self.value = v
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        track_y = h / 2 - 4
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.BORDER))
        p.drawRoundedRect(QRectF(10, track_y - 4, w - 20, 8), 4, 4)
        # green centre zone (+/- 0.15 s), amber to 0.5, red beyond
        cx = w / 2
        scale = (w - 20) / 2.0
        for lo, hi, col in ((0.0, 0.15, theme.GOOD), (0.15, 0.5, theme.WARN), (0.5, 1.0, theme.BAD)):
            c = QColor(col)
            c.setAlpha(60)
            p.setBrush(c)
            p.drawRect(QRectF(cx - hi * scale, track_y - 4, (hi - lo) * scale, 8))
            p.drawRect(QRectF(cx + lo * scale, track_y - 4, (hi - lo) * scale, 8))
        p.setPen(QPen(QColor(theme.MUTED), 1))
        p.drawLine(int(cx), int(track_y - 9), int(cx), int(track_y + 9))
        f = QFont()
        f.setPointSize(8)
        p.setFont(f)
        p.setPen(QPen(QColor(theme.FAINT)))
        p.drawText(QRectF(6, h - 15, 80, 13), Qt.AlignLeft, "-1 s  behind")
        p.drawText(QRectF(w - 86, h - 15, 80, 13), Qt.AlignRight, "ahead  +1 s")
        if self.value is None:
            return
        v = max(-1.0, min(1.0, self.value))
        x = cx + v * scale
        col = theme.GOOD if abs(self.value) <= 0.15 else theme.WARN if abs(self.value) <= 0.5 else theme.BAD
        p.setPen(QPen(QColor(theme.BG), 2))
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
        pen = QPen(QColor(theme.BORDER), 1)
        p.setPen(pen)
        p.drawLine(0, int(mid), w, int(mid))
        for frac in (0.25, 0.75):
            pen.setStyle(Qt.DotLine)
            p.setPen(pen)
            p.drawLine(0, int(h * frac), w, int(h * frac))
        if len(self.values) < 2:
            f = QFont()
            f.setPointSize(8)
            p.setFont(f)
            p.setPen(QPen(QColor(theme.FAINT)))
            p.drawText(self.rect(), Qt.AlignCenter, "drift over the last minute")
            return
        n = self.values.maxlen or 1
        step = w / max(1, n - 1)
        path = QPainterPath()
        fill = QPainterPath()
        pts = []
        for i, v in enumerate(self.values):
            y = mid - max(-1.0, min(1.0, v)) * (h / 2 - 4)
            x = i * step
            pts.append((x, y))
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        fill.addPath(path)
        fill.lineTo(pts[-1][0], mid)
        fill.lineTo(pts[0][0], mid)
        fill.closeSubpath()
        grad = QLinearGradient(0, 0, w, 0)
        for i, c in enumerate(theme.AURORA):
            grad.setColorAt(i / (len(theme.AURORA) - 1), QColor(c))
        fgrad = QLinearGradient(0, 0, 0, h)
        top = QColor(theme.AURORA[0])
        top.setAlpha(60)
        bot = QColor(theme.AURORA[2])
        bot.setAlpha(0)
        fgrad.setColorAt(0, top)
        fgrad.setColorAt(1, bot)
        p.setPen(Qt.NoPen)
        p.setBrush(fgrad)
        p.drawPath(fill)
        p.setPen(QPen(grad, 2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
