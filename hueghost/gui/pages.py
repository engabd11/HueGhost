"""The app's pages. Each gets a Context (daemon + api + toast) and may implement
refresh(status) (called every 500 ms while visible) and on_show()."""
from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QProgressBar,
                               QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget)

from .. import __author__, __url__
from ..config import INTENSITIES, KEEP_AWAKE_MODES
from . import theme
from .widgets import (GLYPHS, Banner, Card, Divider, DriftBar, Poster, Segmented, Sparkline, ToggleSwitch, button,
                      chip, icon, icon_button, icon_family, label, pill, run_async, set_pill)

INTENSITY_OPTIONS = [(i, i.capitalize()) for i in INTENSITIES]
KEEP_AWAKE_OPTIONS = [("off", "Off"), ("playing", "While the ghost plays"), ("always", "Always")]
KEEP_AWAKE_COLORS = {"off": theme.FAINT, "playing": theme.ACCENT, "always": theme.WARN}


@dataclass
class Context:
    daemon: object
    api: object
    toast: Callable[[str, str], None]      # (message, kind: ok|warn|bad)
    goto: Callable[[str], None]            # navigate to a page key


def _fmt_time(s: float | None) -> str:
    if s is None:
        return "--:--"
    s = int(max(0, s))
    h, m, sec = s // 3600, (s // 60) % 60, s % 60
    return "%d:%02d:%02d" % (h, m, sec) if h else "%d:%02d" % (m, sec)


class Page(QWidget):
    key = ""
    title = ""
    subtitle = ""

    def __init__(self, ctx: Context):
        super().__init__()
        self.ctx = ctx
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setObjectName("root")
        self.lay = QVBoxLayout(inner)
        self.lay.setContentsMargins(28, 10, 28, 28)
        self.lay.setSpacing(16)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

    def refresh(self, st: dict) -> None:
        pass

    def on_show(self) -> None:
        pass

    # helpers
    def save(self, partial: dict, ok_msg: str = "Saved") -> None:
        try:
            res = self.ctx.daemon.apply_config(partial)
        except Exception as e:
            self.ctx.toast("Could not save: %s" % e, "bad")
            return
        if res.get("restart_required"):
            self.ctx.toast(ok_msg + " - restart Hue Ghost to apply the network settings", "warn")
        else:
            self.ctx.toast(ok_msg, "ok")

    def footer(self, *widgets: QWidget) -> None:
        """Right-aligned action row at the bottom of a page."""
        row = QHBoxLayout()
        row.addStretch(1)
        for w in widgets:
            row.addWidget(w)
        self.lay.addLayout(row)


# ============================================================================================
class HomePage(Page):
    key, title, subtitle = "home", "Home", "What Hue Ghost is doing right now"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        self.banner = Banner("Setup needed", "warn")
        self.banner.actions.addWidget(button("Set up players", "primary", lambda: ctx.goto("player")))
        self.banner.hide()
        self.lay.addWidget(self.banner)

        # hero
        hero = Card()
        hero.setObjectName("cardHero")
        hero.body.setContentsMargins(24, 22, 24, 22)
        self.hero = hero
        hrow = QHBoxLayout()
        hrow.setSpacing(18)
        self.hero_icon = icon("moon", 30, theme.TEXT)
        self.hero_icon.setFixedWidth(40)
        hrow.addWidget(self.hero_icon, 0, Qt.AlignTop)
        hcol = QVBoxLayout()
        hcol.setSpacing(4)
        self.hero_state = label("Idle", "big")
        self.hero_sub = label("", "muted", wrap=True)
        hcol.addWidget(self.hero_state)
        hcol.addWidget(self.hero_sub)
        hrow.addLayout(hcol, 1)
        hero.body.addLayout(hrow)
        self.lay.addWidget(hero)

        grid = QGridLayout()
        grid.setSpacing(16)
        self.lay.addLayout(grid)

        # now playing
        np_card = Card("Now playing on the TV")
        nrow = QHBoxLayout()
        nrow.setSpacing(16)
        self.poster = Poster(132, 132)
        nrow.addWidget(self.poster, 0, Qt.AlignTop)
        ncol = QVBoxLayout()
        ncol.setSpacing(6)
        self.np_title = label("Nothing", "kpi", wrap=True)
        self.np_device = label("", "muted", wrap=True)
        crow = QHBoxLayout()
        crow.setSpacing(6)
        self.np_flag = chip("")
        self.np_area = chip("", strong=True)
        self.np_flag.hide()
        self.np_area.hide()
        crow.addWidget(self.np_flag)
        crow.addWidget(self.np_area)
        crow.addStretch(1)
        self.np_bar = QProgressBar()
        self.np_bar.setTextVisible(False)
        self.np_bar.setRange(0, 1000)
        self.np_time = label("", "hint")
        ncol.addWidget(self.np_title)
        ncol.addWidget(self.np_device)
        ncol.addLayout(crow)
        ncol.addStretch(1)
        ncol.addWidget(self.np_bar)
        ncol.addWidget(self.np_time)
        nrow.addLayout(ncol, 1)
        np_card.body.addLayout(nrow)
        grid.addWidget(np_card, 0, 0)

        # ghost & drift
        g_card = Card("Ghost lockstep", "how far the ghost is from where the TV really is")
        top = QHBoxLayout()
        self.drift_text = label("drift --", "kpi")
        top.addWidget(self.drift_text)
        top.addStretch(1)
        self.ghost_chips = [chip(""), chip(""), chip(""), chip("")]
        for c in self.ghost_chips:
            top.addWidget(c)
        g_card.body.addLayout(top)
        self.drift = DriftBar()
        self.spark = Sparkline()
        g_card.add(self.drift)
        g_card.add(self.spark)
        grid.addWidget(g_card, 0, 1)

        # hue sync
        h_card = Card("Hue Sync app")
        self.hs_pill = pill("unknown", theme.STATE_COLORS["idle"])
        self.hs_text = label("", "muted", wrap=True)
        h_card.add_row(self.hs_pill, stretch_last=True)
        h_card.add(self.hs_text)
        h_card.add(label("Intensity", "hint"))
        self.intensity = Segmented(INTENSITY_OPTIONS, theme.INTENSITY_COLORS)
        self.intensity.changed.connect(self._set_intensity)
        h_card.add(self.intensity)
        bri = QHBoxLayout()
        bri.addWidget(label("Brightness", "hint"))
        self.bri_val = chip("--")
        bri.addWidget(self.bri_val)
        bri.addStretch(1)
        bri.addWidget(button("-10", "small", lambda: self._bri(-10)))
        bri.addWidget(button("+10", "small", lambda: self._bri(10)))
        h_card.body.addLayout(bri)
        grid.addWidget(h_card, 1, 0)

        # offset + quality
        o_card = Card("Timing", "the ghost runs this far ahead of the TV to cancel the lamp latency")
        orow = QHBoxLayout()
        self.offset_lbl = label("+0.00 s", "kpi")
        orow.addWidget(self.offset_lbl)
        orow.addStretch(1)
        for d, t in ((-0.25, "-0.25"), (-0.05, "-0.05"), (0.05, "+0.05"), (0.25, "+0.25")):
            orow.addWidget(button(t, "small", lambda _=False, dd=d: self._offset(dd)))
        o_card.body.addLayout(orow)
        o_card.add(label("Lights late on a hard cut? increase. Early? decrease. Applies instantly.", "hint", wrap=True))
        o_card.add(Divider())
        self.quality = label("Sync quality: measuring...", "muted", wrap=True)
        o_card.add(self.quality)
        grid.addWidget(o_card, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.lay.addStretch(1)
        self._poster_for: str | None = None
        self._hero_key: tuple | None = None

    def _set_intensity(self, level: str) -> None:
        run_async(lambda: self.ctx.daemon.action("set", {"intensity": level}),
                  on_error=lambda e: self.ctx.toast(e, "bad"))

    def _bri(self, step: int) -> None:
        run_async(lambda: self.ctx.daemon.action("set", {"brightness_step": step}),
                  on_error=lambda e: self.ctx.toast("Brightness: %s" % e, "warn"))

    def _offset(self, delta: float) -> None:
        run_async(lambda: self.ctx.daemon.action("set", {"offset_delta": delta}),
                  on_error=lambda e: self.ctx.toast(e, "bad"))

    def _load_poster(self, item_id: str | None) -> None:
        if item_id == self._poster_for:
            return
        self._poster_for = item_id
        if not item_id:
            self.poster.set_image(None, None)
            return
        jf = self.ctx.daemon.jf

        def done(data, iid=item_id):
            if self._poster_for == iid:
                self.poster.set_image(data, iid)
        run_async(lambda: jf.primary_image(item_id), done, lambda e: None)

    def _set_hero(self, key: str, glyph: str, txt: str, sub: str) -> None:
        if self._hero_key == (key, glyph, txt, sub):
            return
        self._hero_key = (key, glyph, txt, sub)
        col = theme.STATE_COLORS[key]
        if key == "syncing":
            grad = "qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 %s, stop:0.45 %s, stop:1 %s)" % (
                theme.AURORA[0], theme.AURORA[1], theme.CARD)
        else:
            grad = "qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 %s, stop:1 %s)" % (col, theme.CARD)
        self.hero.setStyleSheet("QFrame#cardHero{background: %s; border:none;}" % grad)
        if icon_family():
            self.hero_icon.setText(GLYPHS.get(glyph, ""))
        self.hero_state.setText(txt)
        self.hero_sub.setText(sub)

    def refresh(self, st: dict) -> None:
        setup = st.get("setup_required") or []
        self.banner.setVisible(bool(setup))
        if setup:
            self.banner.text.setText("Setup needed: " + setup[0])
        state = st.get("state", "idle")
        f, g, e, sysinfo = st.get("follow", {}), st.get("ghost", {}), st.get("engine", {}), st.get("system", {})
        locked = bool(sysinfo.get("locked")) and g.get("alive")
        dev = f.get("device") or f.get("device_name_contains") or f.get("device_id") or "the TV"
        if not st.get("enabled"):
            key, glyph, txt, sub = "disabled", "power", "Sync is off", "Turn the switch on (top right) to follow the TV again."
        elif setup:
            key, glyph, txt, sub = "setup", "warn", "Not set up yet", "Pick the Jellyfin player to follow, the ghost display and Hue Sync."
        elif locked:
            key, glyph, txt = "locked", "lock", "PC locked"
            sub = "Hue Sync cannot capture a locked desktop. Sign in and the colours follow at once."
        elif state == "syncing":
            key, glyph, txt = "syncing", "bulb", "Syncing"
            sub = "Hue Sync is streaming the ghost to %s." % (e.get("area_name") or "your entertainment area")
        elif state == "standby":
            key, glyph, txt = "standby", "pause", "Lights off - on standby"
            left = g.get("standby_closes_in_s")
            sub = ("The TV stopped; the ghost waits %s in case it comes straight back."
                   % ("%.0f s" % left if left else "a moment")
                   if g.get("standby") == "stopped" else
                   "The TV has been paused for a while; the lights return the moment it plays.")
        elif state == "ghosting":
            key, glyph, txt = "ghosting", "play", "Ghost playing"
            sub = ("Waiting for Hue Sync to start streaming..." if e.get("connected")
                   else "Hue Sync app not reachable - lights are not driven. " + (e.get("error") or ""))
        else:
            key, glyph, txt = "idle", "moon", "Idle"
            sub = ("Waiting for %s to play something." % dev if f.get("seen")
                   else "%s is not connected to Jellyfin right now." % dev)
            if not st.get("jellyfin", {}).get("ok"):
                sub = "Jellyfin unreachable: %s" % (st.get("jellyfin", {}).get("error") or "?")
                key, glyph = "error", "warn"
        self._set_hero(key, glyph, txt, sub)

        if f.get("playing"):
            self.np_title.setText(f.get("item") or "?")
            self.np_device.setText(f.get("device") or "")
            flag = "paused" if f.get("paused") else "buffering" if f.get("buffering") else ""
            self.np_flag.setText(flag)
            self.np_flag.setVisible(bool(flag))
            self.np_area.setText("→ " + f["area_name"] if f.get("area_name") else "")
            self.np_area.setVisible(bool(f.get("area_name")))
            pos, rt = f.get("position_s") or 0.0, f.get("runtime_s")
            self.np_bar.setValue(int(1000 * pos / rt) if rt else 0)
            self.np_time.setText("%s  /  %s" % (_fmt_time(pos), _fmt_time(rt)))
            self._load_poster(f.get("item_id"))
        else:
            self.np_title.setText("Nothing")
            self.np_device.setText(f.get("device") or dev)
            self.np_flag.hide()
            self.np_area.hide()
            self.np_bar.setValue(0)
            self.np_time.setText("")
            self._load_poster(None)

        d = st.get("drift_s")
        self.drift.set_value(d)
        self.spark.push(d if g.get("alive") else None)
        self.drift_text.setText("drift %+.2f s" % d if d is not None else "drift --")
        if g.get("alive"):
            vals = ("ghost %s" % _fmt_time(g.get("position_s")), "speed %.3f" % (g.get("speed") or 1.0),
                    "seeks %d" % (g.get("seeks") or 0), "nudges %d" % (g.get("nudges") or 0))
        else:
            vals = ("no ghost running", "", "", "")
        for c, v in zip(self.ghost_chips, vals):
            c.setText(v)
            c.setVisible(bool(v))

        if e.get("switching"):
            set_pill(self.hs_pill, "switching area...", theme.WARN)
            self.hs_text.setText("restarting Hue Sync for %s" % (f.get("area_name") or "the bound area"))
        elif e.get("connected"):
            hs_state = e.get("state") or "connected"
            set_pill(self.hs_pill, hs_state.replace("_", " "),
                     theme.GOOD if e.get("syncing") else theme.INFO if hs_state == "bridge_connected" else theme.WARN)
            self.hs_text.setText("area %s   ·   mode %s   ·   intensity %s" % (
                e.get("area_name") or "?", e.get("mode") or "?", e.get("intensity") or "?"))
        else:
            set_pill(self.hs_pill, "not reachable", theme.BAD if e.get("name") == "huesync" else theme.STATE_COLORS["idle"])
            self.hs_text.setText(e.get("error") or ("engine: %s" % e.get("name")))
        self.bri_val.setText("%s %%" % e["bri"] if e.get("bri") is not None else "--")
        self.intensity.set_value(st.get("intensity"))
        self.offset_lbl.setText("%+.2f s" % (st.get("offset_s") or 0.0))
        q = st.get("drift_last_minute")
        if q:
            self.quality.setText("Last minute: mean |drift| %.2f s  ·  p95 %.2f s  ·  max %.2f s  ·  %d seek(s)" % (
                q["mean_abs"], q["p95_abs"], q["max_abs"], q["seeks"]))


# ============================================================================================
class SyncPage(Page):
    key, title, subtitle = "sync", "Sync", "How tightly the ghost follows the TV, and when the lights go off"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        c = Card("Offset", "how far ahead of the TV the ghost runs")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(-200, 500)      # hundredths of a second
        self.slider.setSingleStep(5)
        self.spin = QDoubleSpinBox()
        self.spin.setRange(-2.0, 5.0)
        self.spin.setSingleStep(0.05)
        self.spin.setDecimals(2)
        self.spin.setSuffix(" s")
        self.spin.setFixedWidth(110)
        self.slider.valueChanged.connect(lambda v: self.spin.setValue(v / 100.0))
        self.spin.valueChanged.connect(lambda v: self.slider.setValue(int(round(v * 100))))
        self.spin.editingFinished.connect(self._apply_offset)
        self.slider.sliderReleased.connect(self._apply_offset)
        c.add_row(self.slider, self.spin)
        row = QHBoxLayout()
        for d, t in ((-0.25, "-0.25"), (-0.05, "-0.05"), (0.05, "+0.05"), (0.25, "+0.25")):
            row.addWidget(button(t, "small", lambda _=False, dd=d: self._nudge(dd)))
        row.addStretch(1)
        c.body.addLayout(row)
        c.add(label("The ghost intentionally plays this far ahead of the TV to cancel the capture → bridge → "
                    "lamp latency (and the TV's own display lag). Watch a hard cut: if the lights change late, "
                    "increase it; if early, decrease. Applies instantly.", "hint", wrap=True))
        self.lay.addWidget(c)

        c2 = Card("Intensity for movies", "how strongly Hue Sync reacts to the picture")
        self.intensity = Segmented(INTENSITY_OPTIONS, theme.INTENSITY_COLORS)
        self.intensity.changed.connect(lambda v: run_async(lambda: ctx.daemon.action("set", {"intensity": v}),
                                                           on_error=lambda e: ctx.toast(e, "bad")))
        c2.add(self.intensity)
        c2.add(label("Applied to the Hue Sync session whenever a movie starts (and live while syncing).", "hint", wrap=True))
        self.lay.addWidget(c2)

        c3 = Card("Stopping", "what happens when the TV stops or pauses")
        self.fields: dict[str, QDoubleSpinBox] = {}
        stop_specs = [
            ("sync.lights_off_delay_s", "Lights off after the TV stops", 0.5, 30.0, 0.5, " s",
             "The lights switch off this long after playback stops. Short enough to feel instant, long enough "
             "to ride out a client that briefly drops its session while seeking."),
            ("sync.idle_stop_delay_s", "Close the ghost after", 2.0, 120.0, 1.0, " s",
             "The ghost player stays on standby this long, so a TV that comes straight back (next episode, "
             "a seek that restarts playback) does not need a fresh launch."),
            ("sync.pause_stop_min", "Lights off when paused for", 0.0, 240.0, 1.0, " min",
             "0 = never. Lights stop when the TV has been paused this long and return when it plays again."),
        ]
        for key, name, lo, hi, step, suffix, tip in stop_specs:
            sp = self._spin(lo, hi, step, suffix)
            self.fields[key] = sp
            c3.form(name, sp, tip)
        self.lay.addWidget(c3)

        adv = Card("Advanced lockstep tuning", "defaults are tuned for the 0.5 s Jellyfin poll; change with care")
        specs = [
            ("sync.seek_threshold_s", "Hard-seek beyond", 0.3, 5.0, 0.1, " s", "Drift larger than this jumps the ghost instead of nudging speed."),
            ("sync.deadband_s", "Deadband", 0.0, 0.5, 0.01, " s", "Drift inside this is ignored."),
            ("sync.converge_s", "Converge time", 1.0, 30.0, 0.5, " s", "How quickly small drift is removed by bending speed."),
            ("sync.max_speed_delta", "Max speed change", 0.01, 0.15, 0.01, "", "+/- playback speed used for nudging (0.04 = 4 %)."),
            ("sync.jitter_tolerance_s", "Seek detection", 0.5, 5.0, 0.1, " s", "Position jumps larger than this count as a real seek."),
            ("sync.seek_cooldown_s", "Seek cooldown", 1.0, 15.0, 0.5, " s", "Minimum time between drift-seeks."),
            ("jellyfin.poll_interval_s", "Jellyfin poll", 0.25, 5.0, 0.25, " s", "How often the TV's position is read from Jellyfin."),
        ]
        g = QGridLayout()
        g.setHorizontalSpacing(18)
        g.setVerticalSpacing(8)
        for i, (key, name, lo, hi, step, suffix, tip) in enumerate(specs):
            lb = QLabel(name)
            lb.setObjectName("formLabel")
            lb.setToolTip(tip)
            sp = self._spin(lo, hi, step, suffix)
            sp.setToolTip(tip)
            self.fields[key] = sp
            g.addWidget(lb, i // 2, (i % 2) * 2)
            g.addWidget(sp, i // 2, (i % 2) * 2 + 1)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        adv.body.addLayout(g)
        self.lay.addWidget(adv)

        st_card = Card("Learned TV buffering", "after a seek / start / resume the TV shows a still frame while it buffers")
        self.stalls = label("", "muted", wrap=True)
        st_card.add(self.stalls)
        st_card.add(label("Hue Ghost learns how long, holds the ghost for that long, then resumes exactly on target.",
                          "hint", wrap=True))
        st_card.add_row(button("Reset learned values", "ghost", self._reset_stalls, icon_name="refresh"), stretch_last=True)
        self.lay.addWidget(st_card)
        self.footer(button("Save sync settings", "primary", self._save_adv, icon_name="check"))
        self.lay.addStretch(1)
        self._loaded = False

    @staticmethod
    def _spin(lo, hi, step, suffix) -> QDoubleSpinBox:
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setSingleStep(step)
        sp.setDecimals(2)
        if suffix:
            sp.setSuffix(suffix)
        sp.setFixedWidth(120)
        return sp

    def on_show(self) -> None:
        cfg = self.ctx.daemon.cfg
        for key, sp in self.fields.items():
            sp.blockSignals(True)
            sp.setValue(float(cfg.get(key) or 0.0))
            sp.blockSignals(False)
        self.spin.blockSignals(True)
        self.spin.setValue(float(cfg.get("sync.offset_s", 0.0)))
        self.spin.blockSignals(False)
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(float(cfg.get("sync.offset_s", 0.0)) * 100)))
        self.slider.blockSignals(False)
        self.intensity.set_value(cfg.get("engine.huesync.intensity"))
        self._loaded = True

    def refresh(self, st: dict) -> None:
        est = (st.get("follow") or {}).get("stall_estimates") or {}
        self.stalls.setText("   ".join("%s: %.1f s" % (k, v) for k, v in est.items()) or "-")
        if not self.spin.hasFocus() and not self.slider.isSliderDown():
            v = float(st.get("offset_s") or 0.0)
            if abs(v - self.spin.value()) > 0.001:
                self.spin.blockSignals(True)
                self.spin.setValue(v)
                self.spin.blockSignals(False)
                self.slider.blockSignals(True)
                self.slider.setValue(int(round(v * 100)))
                self.slider.blockSignals(False)
        self.intensity.set_value(st.get("intensity"))

    def _apply_offset(self) -> None:
        v = round(self.spin.value(), 2)
        run_async(lambda: self.ctx.daemon.action("set", {"offset_s": v}), on_error=lambda e: self.ctx.toast(e, "bad"))

    def _nudge(self, d: float) -> None:
        run_async(lambda: self.ctx.daemon.action("set", {"offset_delta": d}), on_error=lambda e: self.ctx.toast(e, "bad"))

    def _save_adv(self) -> None:
        partial: dict = {}
        for key, sp in self.fields.items():
            sect, name = key.split(".", 1)
            partial.setdefault(sect, {})[name] = round(sp.value(), 3)
        self.save(partial, "Sync settings saved")

    def _reset_stalls(self) -> None:
        self.ctx.api.reset_stalls({}, {})
        self.ctx.toast("Learned buffering reset to defaults", "ok")


# ============================================================================================
class PlayerRow(QWidget):
    """One followed player: label + entertainment-area picker + remove."""

    def __init__(self, player: dict, areas: list[dict], on_remove, on_move):
        super().__init__()
        self.player = dict(player)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        name = player.get("device_name_contains") or player.get("device_id") or "?"
        how = "device" if player.get("device_id") else "name match"
        lay.addWidget(icon("tv", 14, theme.MUTED))
        self.lbl = QLabel("%s   <span style='color:%s'>%s%s</span>" % (
            name, theme.MUTED, how, (", user " + player["user"]) if player.get("user") else ""))
        self.lbl.setTextFormat(Qt.RichText)
        lay.addWidget(self.lbl, 1)
        lay.addWidget(label("lights", "hint"))
        self.area = QComboBox()
        self.area.addItem("Hue Sync's current area", "")
        for a in areas:
            self.area.addItem(a["name"], a["id"])
        idx = self.area.findData(player.get("area_id") or "")
        self.area.setCurrentIndex(max(0, idx))
        if player.get("area_id") and idx < 0:
            self.area.addItem(player.get("area_name") or player["area_id"], player["area_id"])
            self.area.setCurrentIndex(self.area.count() - 1)
        self.area.setMinimumWidth(190)
        lay.addWidget(self.area)
        lay.addWidget(icon_button("up", "Higher priority", lambda: on_move(self, -1)))
        lay.addWidget(icon_button("down", "Lower priority", lambda: on_move(self, 1)))
        lay.addWidget(icon_button("remove", "Remove this player", lambda: on_remove(self)))

    def value(self) -> dict:
        p = dict(self.player)
        p["area_id"] = self.area.currentData() or ""
        p["area_name"] = self.area.currentText() if p["area_id"] else ""
        return p


class PlayerPage(Page):
    key, title, subtitle = "player", "Players", "Which Jellyfin clients the ghost follows, and which lights they drive"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        c = Card("Jellyfin server")
        self.url = QLineEdit()
        self.url.setPlaceholderText("http://192.168.0.10:8096")
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("API key (Dashboard > API Keys > +)")
        show = QCheckBox("show")
        show.toggled.connect(lambda on: self.key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        c.form("Server URL", self.url)
        c.form("API key", self.key, trailing=show)
        self.server_status = label("", "muted", wrap=True)
        c.add_row(button("Test connection and list players", "primary", self._test, icon_name="search"),
                  self.server_status, stretch_last=False)
        self.lay.addWidget(c)

        c2 = Card("Players to follow", "top = priority when several play at once")
        c2.add(label("Each player can be bound to an entertainment area: when it plays, Hue Sync switches to those "
                     "lights automatically (the app restarts silently for ~3 s the first time a movie moves rooms). "
                     "'Hue Sync's current area' leaves the selection alone.", "hint", wrap=True))
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(8)
        c2.body.addLayout(self.rows_box)
        self.rows: list[PlayerRow] = []
        self.empty = label("No players yet - add one below.", "muted")
        c2.add(self.empty)
        self.lay.addWidget(c2)

        c3 = Card("Add a player")
        c3.add(label("Play something on the device so it shows up here, select it and add it. Matching by exact "
                     "device is most reliable; matching by name survives app reinstalls.", "hint", wrap=True))
        self.list = QListWidget()
        self.list.setMinimumHeight(150)
        c3.add(self.list)
        self.by_name = QLineEdit()
        self.by_name.setPlaceholderText("device name contains... e.g. Apple TV")
        c3.add_row(button("Add selected player", None, self._add_selected, icon_name="add"), label("or", "hint"),
                   self.by_name, button("Add by name", None, self._add_by_name))
        self.lay.addWidget(c3)
        self.footer(button("Save players", "primary", self._save, icon_name="check"))
        self.lay.addStretch(1)
        self._areas: list[dict] = []

    def on_show(self) -> None:
        cfg = self.ctx.daemon.cfg
        self.url.setText(cfg.get("jellyfin.url") or "")
        self.key.setText(cfg.get("jellyfin.api_key") or "")
        self._areas = list(self.ctx.daemon.engine.areas() or [])
        self._set_rows(cfg.players())
        if self.url.text() and self.key.text() and self.list.count() == 0:
            self._test()

    def _set_rows(self, players: list[dict]) -> None:
        for r in self.rows:
            self.rows_box.removeWidget(r)
            r.deleteLater()
        self.rows = []
        for p in players:
            self._append_row(p)
        self.empty.setVisible(not self.rows)

    def _append_row(self, p: dict) -> None:
        row = PlayerRow(p, self._areas, self._remove, self._move)
        self.rows.append(row)
        self.rows_box.addWidget(row)
        self.empty.setVisible(False)

    def _remove(self, row: PlayerRow) -> None:
        self.rows.remove(row)
        self.rows_box.removeWidget(row)
        row.deleteLater()
        self.empty.setVisible(not self.rows)

    def _move(self, row: PlayerRow, delta: int) -> None:
        i = self.rows.index(row)
        j = i + delta
        if not (0 <= j < len(self.rows)):
            return
        players = [r.value() for r in self.rows]
        players[i], players[j] = players[j], players[i]
        self._set_rows(players)

    def _test(self) -> None:
        url, key = self.url.text().strip(), self.key.text().strip()
        self.server_status.setText("connecting...")

        def done(res):
            info = res["server"]
            self.server_status.setText("%s (Jellyfin %s) - %d session(s)" % (
                info.get("ServerName"), info.get("Version"), len(res["sessions"])))
            self.list.clear()
            for s in res["sessions"]:
                txt = "%s  -  %s%s" % (s["device_name"] or "?", s["client"] or "?",
                                       ("  (%s)" % s["user"]) if s.get("user") else "")
                if s.get("now_playing"):
                    txt += "\n    playing: " + s["now_playing"]
                it = QListWidgetItem(txt)
                it.setData(Qt.UserRole, s)
                self.list.addItem(it)
            if not res["sessions"]:
                self.server_status.setText(self.server_status.text() + " - play something on the TV to see it")

        run_async(lambda: self.ctx.api.sessions({}, {"url": url, "api_key": key}), done,
                  lambda e: self.server_status.setText(e))

    def _add_selected(self) -> None:
        items = self.list.selectedItems()
        if not items:
            self.ctx.toast("Select a player in the list first", "warn")
            return
        s = items[0].data(Qt.UserRole)
        if any(r.player.get("device_id") == s["device_id"] for r in self.rows if s.get("device_id")):
            self.ctx.toast("That player is already in the list", "warn")
            return
        self._append_row({"device_id": s.get("device_id") or "", "device_name_contains": s.get("device_name") or "",
                          "user": "", "area_id": "", "area_name": ""})

    def _add_by_name(self) -> None:
        name = self.by_name.text().strip()
        if not name:
            return
        self._append_row({"device_id": "", "device_name_contains": name, "user": "", "area_id": "", "area_name": ""})
        self.by_name.clear()

    def _save(self) -> None:
        import copy
        from ..config import Config
        players = [r.value() for r in self.rows]
        # work on a copy: apply_config diffs old vs new to decide what to rebuild
        tmp = Config(copy.deepcopy(self.ctx.daemon.cfg.data))
        tmp.set_players(players)
        partial = {"jellyfin": {"url": self.url.text().strip().rstrip("/"), "api_key": self.key.text().strip(),
                                "follow": tmp.get("jellyfin.follow"), "follow_area_id": tmp.get("jellyfin.follow_area_id"),
                                "follow_area_name": tmp.get("jellyfin.follow_area_name"),
                                "players": tmp.get("jellyfin.players")}}
        self.save(partial, "Players saved")


# ============================================================================================
class DisplayPage(Page):
    key, title, subtitle = "display", "Display", "Where the ghost plays (what Hue Sync captures)"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        c = Card("Ghost display")
        c.add(label("Hue Sync captures a whole display, so the ghost needs one of its own to keep your real "
                    "screen free: a virtual display (installed by the setup wizard) or an HDMI dummy plug. "
                    "Pick the same display in Hue Sync > Display.", "hint", wrap=True))
        self.displays = QListWidget()
        self.displays.setMinimumHeight(110)
        c.add(self.displays)
        c.add_row(button("Refresh", "ghost", self._load_displays, icon_name="refresh"),
                  button("Show test pattern for 8 s", None, self._test, icon_name="play"), stretch_last=True)
        self.fullscreen = ToggleSwitch()
        c.form("Fullscreen on that display", self.fullscreen)
        self.geometry = QLineEdit()
        self.geometry.setPlaceholderText("28%x28%-40-40  (windowed size/position)")
        c.form("Window geometry (if not fullscreen)", self.geometry)
        self.lay.addWidget(c)

        c1 = Card("Keep displays awake", "Windows switches every display off after its idle timeout - the ghost display too")
        self.keep_awake = Segmented(KEEP_AWAKE_OPTIONS, KEEP_AWAKE_COLORS)
        c1.add(self.keep_awake)
        c1.add(label("A switched-off display gives Hue Sync nothing to capture: the lights sit on one dim colour "
                     "until someone touches the mouse. 'While the ghost plays' wakes the displays the moment a movie "
                     "starts and lets them sleep again afterwards. The PC itself must not be set to sleep.",
                     "hint", wrap=True))
        self.lay.addWidget(c1)

        c2 = Card("mpv player", "the ghost is a muted mpv, driven over its IPC")
        self.mpv_path = QLineEdit()
        self.mpv_path.setPlaceholderText("mpv (auto-detect)")
        c2.form("mpv path", self.mpv_path, trailing=button("Browse", "ghost", self._browse))
        self.mpv_status = label("", "muted", wrap=True)
        c2.add(self.mpv_status)
        self.hwdec = QComboBox()
        self.hwdec.addItems(["auto", "auto-safe", "d3d11va", "dxva2", "nvdec", "no"])
        self.hwdec.setFixedWidth(160)
        c2.form("Hardware decoding", self.hwdec)
        self.lay.addWidget(c2)
        self.footer(button("Save display settings", "primary", self._save, icon_name="check"))
        self.lay.addStretch(1)

    def on_show(self) -> None:
        cfg = self.ctx.daemon.cfg
        self.fullscreen.setChecked(bool(cfg.get("ghost.fullscreen", True)))
        self.geometry.setText(cfg.get("ghost.geometry") or "")
        self.mpv_path.setText("" if (cfg.get("ghost.mpv_path") in (None, "", "mpv")) else cfg.get("ghost.mpv_path"))
        self.hwdec.setCurrentText(cfg.get("ghost.hwdec") or "auto")
        mode = str(cfg.get("ghost.keep_awake") or "playing").lower()
        self.keep_awake.set_value(mode if mode in KEEP_AWAKE_MODES else "playing")
        self._load_displays()
        self._check_mpv()

    def _load_displays(self) -> None:
        want = (self.ctx.daemon.cfg.get("ghost.screen_name") or "").lower()
        res = self.ctx.api.handle("GET", "/api/displays", {}, {})
        self.displays.clear()
        for d in res["displays"]:
            txt = "%s   %dx%d%s" % (d["name"], d["width"], d["height"], "   (primary - your real screen)" if d["primary"] else "")
            it = QListWidgetItem(txt)
            it.setData(Qt.UserRole, d)
            self.displays.addItem(it)
            if d["name"].lower() == want:
                it.setSelected(True)
        if not res["displays"]:
            self.displays.addItem("no displays enumerated (non-Windows?)")

    def _selected_display(self) -> dict | None:
        items = self.displays.selectedItems()
        return items[0].data(Qt.UserRole) if items else None

    def _test(self) -> None:
        d = self._selected_display()
        run_async(lambda: self.ctx.daemon.test_ghost(d["name"] if d else None, 8),
                  lambda r: self.ctx.toast("Test pattern showing for %d s" % r["seconds"], "ok"),
                  lambda e: self.ctx.toast(e, "bad"))

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Locate mpv", "", "mpv (mpv.exe mpv);;All files (*)")
        if path:
            self.mpv_path.setText(path)
            self._check_mpv()

    def _check_mpv(self) -> None:
        p = self.mpv_path.text().strip() or "mpv"

        def done(res):
            self.mpv_status.setText(("found: %s\n%s" % (res["path"], res["version"] or "")) if res["found"]
                                    else "mpv not found - install it from https://mpv.io or point to mpv.exe")
        run_async(lambda: self.ctx.api.mpv({}, {"path": p}), done, lambda e: self.mpv_status.setText(e))

    def _save(self) -> None:
        d = self._selected_display()
        partial = {"ghost": {"fullscreen": self.fullscreen.isChecked(), "geometry": self.geometry.text().strip() or "28%x28%-40-40",
                             "mpv_path": self.mpv_path.text().strip() or "mpv", "hwdec": self.hwdec.currentText(),
                             "keep_awake": self.keep_awake.value() or "playing",
                             "screen_name": d["name"] if d else "", "screen_index": d["index"] if d else None}}
        self.save(partial, "Display settings saved (used at the next playback)")


# ============================================================================================
class HueSyncPage(Page):
    key, title, subtitle = "huesync", "Hue Sync", "The official Hue Sync app turns the ghost into light"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        c = Card("Status")
        self.app_status = label("", "muted", wrap=True)
        self.probe = pill("checking...", theme.STATE_COLORS["idle"])
        c.add_row(self.probe, button("Re-check", "ghost", self._probe, icon_name="refresh"), stretch_last=True)
        c.add(self.app_status)
        self.lay.addWidget(c)

        c2 = Card("Checklist", "in the Hue Sync app, once")
        for t in ("Settings > Allow public control: ON  (this is how Hue Ghost starts and stops sync)",
                  "Display: pick the ghost display (the virtual display / dummy plug)",
                  "Select the entertainment area of the room where the TV is",
                  "Start syncing when Hue Sync launches: OFF  (it would sync your desktop)"):
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(icon("check", 13, theme.ACCENT), 0, Qt.AlignTop)
            row.addWidget(label(t, "muted", wrap=True), 1)
            c2.body.addLayout(row)
        self.lay.addWidget(c2)

        c3 = Card("Engine")
        self.engine = QComboBox()
        self.engine.addItem("Hue Sync app (recommended)", "huesync")
        self.engine.addItem("HTTP hook (GET url/start, url/stop)", "httphook")
        self.engine.addItem("None - I start sync myself", "none")
        self.engine.setMinimumWidth(280)
        c3.form("Light engine", self.engine)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setFixedWidth(110)
        c3.form("Public control port", self.port)
        self.mode = QComboBox()
        self.mode.addItems(["video", "games", "music"])
        self.mode.setFixedWidth(140)
        c3.form("Hue Sync mode for movies", self.mode)
        self.launch = ToggleSwitch()
        c3.form("Start Hue Sync if it is not running", self.launch)
        self.required = ToggleSwitch()
        c3.form("Only run the ghost when Hue Sync is reachable", self.required)
        self.hook_url = QLineEdit()
        self.hook_url.setPlaceholderText("http://127.0.0.1:8989")
        c3.form("HTTP hook URL", self.hook_url)
        self.lay.addWidget(c3)
        self.footer(button("Save Hue Sync settings", "primary", self._save, icon_name="check"))
        self.lay.addStretch(1)

    def on_show(self) -> None:
        cfg = self.ctx.daemon.cfg
        idx = self.engine.findData(cfg.get("engine.type") or "huesync")
        self.engine.setCurrentIndex(max(0, idx))
        self.port.setValue(int(cfg.get("engine.huesync.port") or 24851))
        self.mode.setCurrentText(cfg.get("engine.huesync.mode") or "video")
        self.launch.setChecked(bool(cfg.get("engine.huesync.launch_exe")))
        self.required.setChecked(bool(cfg.get("engine.huesync.required")))
        self.hook_url.setText(cfg.get("engine.httphook.url") or "")
        self._probe()

    def _probe(self) -> None:
        def done(info):
            pr = info.get("probe") or {}
            if pr.get("reachable") and pr.get("state"):
                s = pr["state"]
                set_pill(self.probe, "public control: %s" % s.get("state", "?").replace("_", " "),
                         theme.GOOD if s.get("state") != "bridge_disconnected" else theme.BAD)
            elif pr.get("reachable"):
                set_pill(self.probe, "reachable, no state yet", theme.WARN)
            else:
                set_pill(self.probe, "not reachable", theme.BAD)
            lines = []
            lines.append("App: %s" % (info.get("exe") or "not found in the usual place"))
            if info.get("public_control_enabled") is not None:
                lines.append("Allow public control: %s (port %s)" % (
                    "ON" if info["public_control_enabled"] else "OFF - enable it in Hue Sync > Settings", info.get("public_control_port")))
            if info.get("selected_area"):
                lines.append("Entertainment area selected in Hue Sync: %s" % info["selected_area"])
            if info.get("automatic_display"):
                lines.append("Display: Automatic - pick the ghost display manually in Hue Sync > Display")
            if pr.get("error") and not pr.get("reachable"):
                lines.append("Probe: %s" % pr["error"])
            self.app_status.setText("\n".join(lines))
        run_async(lambda: self.ctx.api.huesync({}, {"port": str(self.port.value())}), done,
                  lambda e: self.app_status.setText(e))

    def _save(self) -> None:
        from ..winutil import hue_sync_exe
        partial = {"engine": {"type": self.engine.currentData(),
                              "huesync": {"port": int(self.port.value()), "mode": self.mode.currentText(),
                                          "launch_exe": (hue_sync_exe() or "") if self.launch.isChecked() else "",
                                          "required": self.required.isChecked()},
                              "httphook": {"url": self.hook_url.text().strip()}}}
        self.save(partial, "Hue Sync settings saved")
        self._probe()


# ============================================================================================
class HomeAssistantPage(Page):
    key, title, subtitle = "ha", "Home Assistant", "Control Hue Ghost from your smart home"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        self.restart_banner = Banner("Network settings changed - restart Hue Ghost to apply them.", "info")
        self.restart_banner.actions.addWidget(button("Restart now", "primary", self._restart))
        self.restart_banner.hide()
        self.lay.addWidget(self.restart_banner)
        c = Card("Control API")
        self.expose = ToggleSwitch()
        c.form("Allow control from the LAN", self.expose, "Home Assistant, a phone, curl - anything on your network.")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setFixedWidth(110)
        c.form("Port", self.port)
        self.token = QLineEdit()
        self.token.setPlaceholderText("token (required when exposed)")
        tok_btns = QWidget()
        tb = QHBoxLayout(tok_btns)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(6)
        tb.addWidget(button("Generate", "ghost", self._gen))
        tb.addWidget(button("Copy", "ghost", self._copy, icon_name="copy"))
        c.form("Token", self.token, trailing=tok_btns)
        self.addr = label("", "muted", wrap=True)
        c.add(self.addr)
        self.lay.addWidget(c)
        c2 = Card("Hue Synco integration", "recommended")
        c2.add(label("In Home Assistant: Settings > Devices & services > Hue Synco > Configure > enter this PC's "
                     "address, port and token. You get a Movie mode switch, a state sensor, an intensity select and "
                     "a sync-offset number - and movie mode hands the entertainment area over from music sync "
                     "automatically.", "muted", wrap=True))
        c2.add(label("Without Hue Synco: POST /on, /off, /set {\"offset_delta\": 0.25}; GET /status. "
                     "Header: Authorization: Bearer <token>.", "hint", wrap=True))
        self.lay.addWidget(c2)
        self.footer(button("Save network settings", "primary", self._save, icon_name="check"))
        self.lay.addStretch(1)

    def on_show(self) -> None:
        cfg = self.ctx.daemon.cfg
        self.expose.setChecked((cfg.get("control.bind") or "127.0.0.1") not in ("127.0.0.1", "localhost"))
        self.port.setValue(int(cfg.get("control.port") or 8787))
        self.token.setText(cfg.get("control.token") or "")
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            ip = "?"
        self.addr.setText("This PC: %s  →  http://%s:%d" % (socket.gethostname(), ip, self.port.value()))

    def refresh(self, st: dict) -> None:
        self.restart_banner.setVisible("control" in (st.get("restart_required") or []))

    def _gen(self) -> None:
        self.token.setText(self.ctx.api.handle("POST", "/api/token", {}, {})["token"])

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.token.text())
        self.ctx.toast("Token copied", "ok")

    def _save(self) -> None:
        expose = self.expose.isChecked()
        if expose and not self.token.text().strip():
            self._gen()
        self.save({"control": {"bind": "0.0.0.0" if expose else "127.0.0.1", "port": int(self.port.value()),
                               "token": self.token.text().strip()}}, "Network settings saved")

    def _restart(self) -> None:
        self.ctx.api.restart({}, {})


# ============================================================================================
CREDITS = [
    ("mpv", "plays the ghost - the muted player driven in lockstep. GPLv2+, mpv.io"),
    ("Philips Hue Sync", "the official desktop app that turns the ghost display into light (Signify). "
                         "Hue Ghost is not affiliated with or endorsed by Signify / Philips Hue."),
    ("Jellyfin", "the media server whose sessions tell Hue Ghost what the TV is watching. GPLv2"),
    ("Virtual Display Driver", "the signed virtual display the ghost plays on (VirtualDrivers). MIT"),
    ("Qt / PySide6", "this desktop app. LGPLv3"),
    ("PyInstaller & Inno Setup", "the Windows build and the installer wizard"),
    ("Home Assistant & Hue Synco", "movie mode from your smart home, hand-over from music sync"),
]


class SettingsPage(Page):
    key, title, subtitle = "settings", "Settings", "App behaviour, diagnostics and credits"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        c = Card("Startup")
        self.autostart = ToggleSwitch()
        self.autostart.toggled.connect(self._autostart)
        c.form("Start Hue Ghost when I sign in", self.autostart, "Minimised to the tray; the lights follow the TV without opening anything.")
        self.lay.addWidget(c)
        c2 = Card("Diagnostics")
        self.level = QComboBox()
        self.level.addItems(["INFO", "DEBUG", "WARNING"])
        self.level.setFixedWidth(140)
        self.level.currentTextChanged.connect(lambda v: self.save({"log_level": v}, "Log level: " + v))
        c2.form("Log level", self.level)
        c2.add_row(button("Open log file", "ghost", lambda: self._open("log"), icon_name="log"),
                   button("Open config folder", "ghost", lambda: self._open("folder"), icon_name="folder"), stretch_last=True)
        self.paths = label("", "faint", wrap=True)
        c2.add(self.paths)
        self.lay.addWidget(c2)

        about = Card()
        arow = QHBoxLayout()
        arow.setSpacing(18)
        from .app import ghost_pixmap
        mark = QLabel()
        mark.setPixmap(ghost_pixmap(None, 64))
        mark.setFixedSize(64, 64)
        arow.addWidget(mark, 0, Qt.AlignTop)
        acol = QVBoxLayout()
        acol.setSpacing(3)
        acol.addWidget(label("Hue Ghost", "big"))
        acol.addWidget(label("A software Hue Sync Box for Jellyfin: the PC plays a muted ghost of what the TV plays, "
                             "in lockstep, and the official Hue Sync app turns it into light.", "muted", wrap=True))
        self.version = label("", "hint")
        acol.addWidget(self.version)
        acol.addSpacing(6)
        built = QHBoxLayout()
        built.setSpacing(8)
        built.addWidget(icon("heart", 13, theme.ACCENT))
        built.addWidget(label("Built by", "muted"))
        built.addWidget(label(__author__, "kpiSmall"))
        built.addStretch(1)
        acol.addLayout(built)
        acol.addWidget(label(__url__, "link"))
        acol.addWidget(label("MIT licensed. Not affiliated with Signify / Philips Hue or Jellyfin.", "hint", wrap=True))
        arow.addLayout(acol, 1)
        about.body.addLayout(arow)
        self.lay.addWidget(about)

        cr = Card("Credits", "Hue Ghost stands on these projects")
        for name, note in CREDITS:
            row = QHBoxLayout()
            row.setSpacing(12)
            n = label(name, "creditName")
            n.setFixedWidth(210)
            row.addWidget(n, 0, Qt.AlignTop)
            row.addWidget(label(note, "creditNote", wrap=True), 1)
            cr.body.addLayout(row)
        self.lay.addWidget(cr)
        self.lay.addStretch(1)

    def on_show(self) -> None:
        info = self.ctx.api.system({}, {})
        self.autostart.blockSignals(True)
        self.autostart.setChecked(bool(info["autostart"]))
        self.autostart.blockSignals(False)
        self.level.blockSignals(True)
        self.level.setCurrentText(str(self.ctx.daemon.cfg.get("log_level") or "INFO").upper())
        self.level.blockSignals(False)
        self.paths.setText("config: %s\nlog: %s" % (info["config_path"], info["log_path"]))
        self.version.setText("version %s%s" % (info["version"], "  ·  installed build" if info["frozen"] else "  ·  running from source"))

    def _autostart(self, on: bool) -> None:
        try:
            self.ctx.api.autostart({"enabled": on}, {})
            self.ctx.toast("Autostart %s" % ("enabled" if on else "disabled"), "ok")
        except Exception as e:
            self.ctx.toast(str(e), "bad")

    def _open(self, what: str) -> None:
        try:
            self.ctx.api.open_thing({"what": what}, {})
        except Exception as e:
            self.ctx.toast(str(e), "bad")


# ============================================================================================
LEVEL_COLORS = {"ERROR": theme.BAD, "WARNING": theme.WARN, "DEBUG": theme.FAINT, "INFO": theme.TEXT_2}


class LogPage(Page):
    key, title, subtitle = "log", "Log", "What the daemon is doing (newest at the bottom)"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter lines...")
        self.filter.textChanged.connect(lambda _: self._render())
        bar.addWidget(self.filter, 1)
        bar.addWidget(button("Copy", "ghost", self._copy, icon_name="copy"))
        bar.addWidget(button("Open file", "ghost", lambda: ctx.api.open_thing({"what": "log"}, {}), icon_name="open"))
        self.lay.addLayout(bar)
        self.text = QPlainTextEdit()
        self.text.setObjectName("log")
        self.text.setReadOnly(True)
        self.text.setMinimumHeight(480)
        self.lay.addWidget(self.text, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._load)
        self._lines: list[str] = []
        self._rendered: tuple[tuple[str, ...], str] | None = None

    def on_show(self) -> None:
        self._load()
        self.timer.start()

    def hideEvent(self, e) -> None:  # noqa: N802
        self.timer.stop()
        super().hideEvent(e)

    def _load(self) -> None:
        res = self.ctx.api.log_tail({}, {"lines": "400"})
        self._lines = res["lines"]
        self._render()

    def _render(self) -> None:
        needle = self.filter.text().strip().lower()
        lines = [ln for ln in self._lines if not needle or needle in ln.lower()]
        key = (tuple(lines), needle)
        if key == self._rendered:
            return
        self._rendered = key
        self.text.clear()
        cur = self.text.textCursor()
        for ln in lines:
            fmt = QTextCharFormat()
            parts = ln.split()
            lvl = parts[1] if len(parts) > 1 else ""
            fmt.setForeground(QColor(LEVEL_COLORS.get(lvl, theme.TEXT_2)))
            cur.insertText(ln + "\n", fmt)
        cur.movePosition(QTextCursor.End)
        self.text.setTextCursor(cur)
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.text.toPlainText())
        self.ctx.toast("Log copied", "ok")


PAGES = [HomePage, SyncPage, PlayerPage, DisplayPage, HueSyncPage, HomeAssistantPage, SettingsPage, LogPage]
