"""The mouse wheel scrolls the page; it never changes a setting it passes over.

Needs PySide6 (the ``gui`` extra); skipped where it is not installed."""
import os
import sys

import pytest

pytest.importorskip("PySide6")
if sys.platform != "win32":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import (QApplication, QComboBox, QScrollArea, QSlider, QSpinBox,  # noqa: E402
                               QVBoxLayout, QWidget)

from hueghost.gui.widgets import install_wheel_guard  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _page():
    area = QScrollArea()
    area.setWidgetResizable(True)
    body = QWidget()
    lay = QVBoxLayout(body)
    combo = QComboBox()
    combo.addItems(["Living room", "Office", "Gaming desk"])
    spin = QSpinBox()
    spin.setRange(0, 100)
    spin.setValue(50)
    slider = QSlider(Qt.Horizontal)
    slider.setRange(0, 100)
    slider.setValue(50)
    for w in (combo, spin, slider):
        lay.addWidget(w)
    lay.addSpacing(3000)                 # tall enough to scroll
    area.setWidget(body)
    area.resize(400, 300)
    area.show()
    return area, combo, spin, slider


def _wheel_down(w):
    pos = QPointF(w.width() / 2, w.height() / 2)
    ev = QWheelEvent(pos, QPointF(w.mapToGlobal(pos.toPoint())), QPoint(), QPoint(0, -120),
                     Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(w, ev)


def test_wheel_over_controls_scrolls_the_page_and_changes_nothing(app):
    guard = install_wheel_guard(app)
    try:
        area, combo, spin, slider = _page()
        bar = area.verticalScrollBar()
        for w in (combo, spin, slider):
            _wheel_down(w)
        assert combo.currentIndex() == 0
        assert spin.value() == 50 and slider.value() == 50
        assert bar.value() > 0, "the page scrolled instead"
        area.close()
    finally:
        app.removeEventFilter(guard)


def test_without_the_guard_qt_would_have_changed_them(app):
    """Proves the test above tests something: stock Qt does change values."""
    area, combo, spin, slider = _page()
    _wheel_down(combo)
    _wheel_down(spin)
    assert combo.currentIndex() == 1 or spin.value() != 50
    area.close()
