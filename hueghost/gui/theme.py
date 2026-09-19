"""Dark, colourful look in the spirit of the Hue Sync desktop app."""
from __future__ import annotations

BG = "#0f1115"
PANEL = "#171a21"
CARD = "#1f232c"
CARD_HI = "#262b36"
BORDER = "#2c3140"
TEXT = "#f3f5f9"
MUTED = "#98a2b3"
ACCENT = "#7c5cff"
ACCENT2 = "#ff5c8a"
GOOD = "#22c55e"
WARN = "#f59e0b"
BAD = "#ef4444"
INFO = "#3b82f6"

STATE_COLORS = {
    "idle": "#6b7280",
    "ghosting": INFO,
    "syncing": GOOD,
    "disabled": "#4b5563",
    "setup": WARN,
    "error": BAD,
}

INTENSITY_COLORS = {
    "subtle": "#38bdf8",
    "moderate": "#a78bfa",
    "high": "#f472b6",
    "extreme": "#fb7185",
}

QSS = f"""
* {{ font-family: "Segoe UI", "Inter", system-ui; font-size: 13px; color: {TEXT}; }}
QMainWindow, QWidget#root {{ background: {BG}; }}
QFrame#nav {{ background: {PANEL}; border-right: 1px solid {BORDER}; }}
QLabel#brand {{ font-size: 20px; font-weight: 700; padding: 18px 16px 6px 16px; }}
QLabel#brandSub {{ color: {MUTED}; padding: 0 16px 14px 16px; font-size: 11px; }}
QPushButton#navBtn {{
    text-align: left; padding: 10px 16px; border: none; border-radius: 10px; margin: 2px 10px;
    background: transparent; color: {MUTED}; font-size: 14px;
}}
QPushButton#navBtn:hover {{ background: {CARD}; color: {TEXT}; }}
QPushButton#navBtn:checked {{ background: {CARD_HI}; color: {TEXT}; font-weight: 600; }}
QLabel#pageTitle {{ font-size: 22px; font-weight: 700; }}
QLabel#pageSub {{ color: {MUTED}; }}
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
QLabel#cardTitle {{ color: {MUTED}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
QLabel#big {{ font-size: 26px; font-weight: 700; }}
QLabel#kpi {{ font-size: 20px; font-weight: 700; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#hint {{ color: {MUTED}; font-size: 12px; }}
QLabel#pill {{ padding: 4px 12px; border-radius: 11px; font-weight: 600; font-size: 12px; color: white; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QListWidget {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}
QListWidget::item {{ padding: 8px; border-radius: 8px; }}
QListWidget::item:selected {{ background: {ACCENT}; color: white; }}
QListWidget::item:hover {{ background: {CARD_HI}; }}
QPushButton {{
    background: {CARD_HI}; border: 1px solid {BORDER}; border-radius: 9px; padding: 7px 14px; font-weight: 600;
}}
QPushButton:hover {{ background: #2f3542; }}
QPushButton:pressed {{ background: #3a4151; }}
QPushButton:disabled {{ color: #6b7280; }}
QPushButton#primary {{ background: {ACCENT}; border: none; color: white; }}
QPushButton#primary:hover {{ background: #8f74ff; }}
QPushButton#danger {{ background: {BAD}; border: none; color: white; }}
QPushButton#ghost {{ background: transparent; border: 1px solid {BORDER}; }}
QPushButton#small {{ padding: 4px 10px; font-size: 12px; }}
QSlider::groove:horizontal {{ height: 6px; background: {BORDER}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ width: 18px; height: 18px; margin: -6px 0; border-radius: 9px; background: white; }}
QProgressBar {{ background: {BORDER}; border: none; border-radius: 4px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ACCENT}, stop:1 {ACCENT2}); border-radius: 4px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER}; background: {PANEL}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 10px; margin-top: 10px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {MUTED}; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER}; }}
QFrame#banner {{ background: #3b2f10; border: 1px solid {WARN}; border-radius: 10px; }}
QFrame#bannerInfo {{ background: #172554; border: 1px solid {INFO}; border-radius: 10px; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; }}
QMenu::item {{ padding: 6px 18px; }}
QMenu::item:selected {{ background: {ACCENT}; }}
"""
