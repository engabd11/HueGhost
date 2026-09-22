"""Design tokens + stylesheet.

Dark, Hue-inspired, with the Cyborg Automation AU brand: a warm charcoal base
and a gold accent for everything the user touches; the Hue "aurora" gradient
(violet -> magenta -> gold) for everything that stands for light itself.
"""
from __future__ import annotations

# -- base ----------------------------------------------------------------------------
BG = "#0f0e0b"          # window
PANEL = "#151310"       # nav rail, inputs
CARD = "#1c1a16"
CARD_HI = "#26231d"     # hover / raised
BORDER = "#302c25"
BORDER_HI = "#403b32"
TEXT = "#f5f3ef"
TEXT_2 = "#c9c3b8"
MUTED = "#8f887c"
FAINT = "#5e584f"

# -- brand ------------------------------------------------------------------------------
ACCENT = "#e4a667"      # gold
ACCENT_HI = "#efbf8a"
ACCENT_LO = "#c88b4d"   # bronze (pressed)
ACCENT_FAINT = "#2b2419"  # gold at ~10 % on the card colour
GLOW = "rgba(228, 166, 103, 0.38)"

# -- hue aurora (light = colour) ------------------------------------------------------------
AURORA = ("#8b5cf6", "#ec4899", "#e4a667")
ACCENT2 = AURORA[1]

# -- semantic ---------------------------------------------------------------------------------
GOOD = "#3ddc97"
WARN = "#fb923c"
BAD = "#e05050"
INFO = "#60a5fa"

STATE_COLORS = {
    "idle": "#6b665d",
    "ghosting": INFO,
    "syncing": GOOD,
    "standby": "#a78bfa",
    "disabled": "#4a463f",
    "setup": WARN,
    "locked": WARN,
    "error": BAD,
}

STATE_LABELS = {
    "idle": "idle",
    "ghosting": "ghost playing",
    "syncing": "syncing",
    "standby": "standby",
    "disabled": "sync off",
    "setup": "setup needed",
    "locked": "PC locked",
    "error": "Jellyfin unreachable",
}

INTENSITY_COLORS = {
    "subtle": "#38bdf8",
    "moderate": "#a78bfa",
    "high": "#f472b6",
    "extreme": "#fb7185",
}

FONT = '"Segoe UI Variable Text", "Segoe UI", "Inter", system-ui, sans-serif'
FONT_DISPLAY = '"Segoe UI Variable Display", "Segoe UI", "Inter", system-ui, sans-serif'
ICON_FONTS = ("Segoe Fluent Icons", "Segoe MDL2 Assets")


def aurora_css(angle: str = "x1:0,y1:0,x2:1,y2:0") -> str:
    return "qlineargradient(%s, stop:0 %s, stop:0.55 %s, stop:1 %s)" % (angle, *AURORA)


QSS = f"""
* {{ font-family: {FONT}; font-size: 13px; color: {TEXT}; }}
QMainWindow, QWidget#root {{ background: {BG}; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER_HI}; padding: 6px 8px; border-radius: 6px; }}

/* nav rail */
QFrame#nav {{ background: {PANEL}; border-right: 1px solid {BORDER}; }}
QLabel#brand {{ font-family: {FONT_DISPLAY}; font-size: 19px; font-weight: 700; letter-spacing: 0.2px; }}
QLabel#brandSub {{ color: {MUTED}; font-size: 11px; }}
QLabel#brandFoot {{ color: {FAINT}; font-size: 11px; }}
QLabel#brandFootStrong {{ color: {MUTED}; font-size: 11px; font-weight: 600; }}
QPushButton#navBtn {{
    text-align: left; padding: 0; margin: 0; border: none; border-radius: 10px;
    background: transparent; color: {TEXT_2}; font-size: 13.5px;
}}
QPushButton#navBtn:hover {{ background: {CARD}; color: {TEXT}; }}
QPushButton#navBtn:checked {{ background: {ACCENT_FAINT}; color: {TEXT}; font-weight: 600; }}
QLabel#navIcon {{ font-size: 15px; color: {MUTED}; }}
QLabel#navIcon[active="true"] {{ color: {ACCENT}; }}
QFrame#navMark {{ background: transparent; }}

/* header */
QLabel#pageTitle {{ font-family: {FONT_DISPLAY}; font-size: 22px; font-weight: 700; }}
QLabel#pageSub {{ color: {MUTED}; }}
QLabel#headerLabel {{ color: {MUTED}; font-weight: 600; }}

/* cards */
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
QFrame#cardHero {{ border: none; border-radius: 16px; }}
QLabel#cardTitle {{ color: {MUTED}; font-size: 11px; font-weight: 700; letter-spacing: 1.2px; }}
QLabel#cardSub {{ color: {FAINT}; font-size: 12px; }}
QLabel#big {{ font-family: {FONT_DISPLAY}; font-size: 28px; font-weight: 700; }}
QLabel#kpi {{ font-family: {FONT_DISPLAY}; font-size: 21px; font-weight: 700; }}
QLabel#kpiSmall {{ font-family: {FONT_DISPLAY}; font-size: 16px; font-weight: 600; }}
QLabel#muted {{ color: {TEXT_2}; }}
QLabel#hint {{ color: {MUTED}; font-size: 12px; }}
QLabel#faint {{ color: {FAINT}; font-size: 12px; }}
QLabel#formLabel {{ color: {TEXT_2}; }}
QLabel#chip {{ background: {CARD_HI}; border: 1px solid {BORDER}; border-radius: 9px; padding: 3px 9px;
               color: {TEXT_2}; font-size: 12px; }}
QLabel#chipStrong {{ background: {CARD_HI}; border: 1px solid {BORDER}; border-radius: 9px; padding: 3px 9px;
                     color: {TEXT}; font-size: 12px; font-weight: 600; }}
QLabel#pill {{ padding: 4px 12px; border-radius: 11px; font-weight: 600; font-size: 12px; color: white; }}
QLabel#poster {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; }}
QLabel#link {{ color: {ACCENT}; }}
QLabel#creditName {{ font-weight: 600; }}
QLabel#creditNote {{ color: {MUTED}; font-size: 12px; }}

/* inputs */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QListWidget {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 9px; padding: 6px 10px;
    selection-background-color: {ACCENT}; selection-color: {BG}; min-height: 20px;
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{ border-color: {BORDER_HI}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{ color: {FAINT}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: none; width: 0; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {BORDER_HI}; border-radius: 8px;
                                selection-background-color: {ACCENT}; selection-color: {BG}; padding: 4px; outline: 0; }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button {{
    width: 18px; border: none; background: transparent; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ width: 8px; height: 8px; }}
QListWidget {{ padding: 4px; outline: 0; }}
QListWidget::item {{ padding: 8px 10px; border-radius: 8px; color: {TEXT_2}; }}
QListWidget::item:selected {{ background: {ACCENT}; color: {BG}; }}
QListWidget::item:hover:!selected {{ background: {CARD_HI}; color: {TEXT}; }}

/* buttons */
QPushButton {{
    background: {CARD_HI}; border: 1px solid {BORDER}; border-radius: 9px; padding: 7px 14px; font-weight: 600;
    color: {TEXT};
}}
QPushButton:hover {{ background: #2e2a23; border-color: {BORDER_HI}; }}
QPushButton:pressed {{ background: #35302a; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {FAINT}; background: {CARD}; }}
QPushButton#primary {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: {BG}; }}
QPushButton#primary:hover {{ background: {ACCENT_HI}; border-color: {ACCENT_HI}; }}
QPushButton#primary:pressed {{ background: {ACCENT_LO}; border-color: {ACCENT_LO}; }}
QPushButton#primary:disabled {{ background: {CARD_HI}; border-color: {BORDER}; color: {FAINT}; }}
QPushButton#danger {{ background: {BAD}; border: 1px solid {BAD}; color: white; }}
QPushButton#danger:hover {{ background: #e86a6a; }}
QPushButton#ghost {{ background: transparent; border: 1px solid {BORDER_HI}; color: {TEXT_2}; }}
QPushButton#ghost:hover {{ background: {CARD_HI}; color: {TEXT}; }}
QPushButton#small {{ padding: 4px 10px; font-size: 12px; }}
QPushButton#icon {{ padding: 4px 8px; min-width: 20px; }}

/* sliders, progress */
QSlider::groove:horizontal {{ height: 6px; background: {BORDER}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ width: 18px; height: 18px; margin: -6px 0; border-radius: 9px; background: {TEXT};
                              border: 2px solid {ACCENT}; }}
QSlider::handle:horizontal:hover {{ background: white; }}
QProgressBar {{ background: {BORDER}; border: none; border-radius: 4px; max-height: 8px; min-height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {aurora_css()}; border-radius: 4px; }}

/* scroll */
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_HI}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {FAINT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_HI}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* misc */
QCheckBox {{ spacing: 8px; color: {TEXT_2}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER_HI}; background: {PANEL}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 12px; margin-top: 12px; padding: 14px 10px 6px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 6px; color: {MUTED}; font-size: 11px;
                    font-weight: 700; letter-spacing: 1.2px; }}
QFrame#banner {{ background: #3a2a14; border: 1px solid {WARN}; border-radius: 12px; }}
QFrame#bannerInfo {{ background: #1a2740; border: 1px solid {INFO}; border-radius: 12px; }}
QFrame#bannerBad {{ background: #3d1c1c; border: 1px solid {BAD}; border-radius: 12px; }}
QFrame#divider {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER_HI}; border-radius: 8px; padding: 6px; }}
QMenu::item {{ padding: 7px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT}; color: {BG}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 6px 4px; }}
QPlainTextEdit#log {{ font-family: Cascadia Mono, Consolas, monospace; font-size: 12px; background: {PANEL};
                      border: 1px solid {BORDER}; border-radius: 12px; padding: 10px; }}
"""
