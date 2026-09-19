"""The app's pages. Each gets a Context (daemon + api + toast) and may implement
refresh(status) (called every 500 ms while visible) and on_show()."""
from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
                               QProgressBar, QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget)

from ..config import INTENSITIES
from . import theme
from .widgets import (Banner, Card, DriftBar, Segmented, Sparkline, ToggleSwitch, button, label, pill,
                      run_async, set_pill)

INTENSITY_OPTIONS = [(i, i.capitalize()) for i in INTENSITIES]


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
        self.lay.setContentsMargins(24, 8, 24, 24)
        self.lay.setSpacing(14)
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


# ============================================================================================
class HomePage(Page):
    key, title, subtitle = "home", "Home", "What Hue Ghost is doing right now"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        self.banner = Banner("Setup needed", "warn")
        self.banner.actions.addWidget(button("Set up player", "primary", lambda: ctx.goto("player")))
        self.banner.hide()
        self.lay.addWidget(self.banner)

        hero = Card()
        self.hero = hero
        self.hero_state = label("Idle", "big")
        self.hero_sub = label("", "muted", wrap=True)
        hero.add(self.hero_state)
        hero.add(self.hero_sub)
        self.lay.addWidget(hero)

        grid = QGridLayout()
        grid.setSpacing(14)
        self.lay.addLayout(grid)

        # now playing
        np_card = Card("Now playing on the TV")
        self.np_title = label("Nothing", "kpi", wrap=True)
        self.np_device = label("", "muted")
        self.np_bar = QProgressBar()
        self.np_bar.setTextVisible(False)
        self.np_bar.setRange(0, 1000)
        self.np_time = label("", "muted")
        np_card.add(self.np_title)
        np_card.add(self.np_device)
        np_card.add(self.np_bar)
        np_card.add(self.np_time)
        grid.addWidget(np_card, 0, 0)

        # ghost & drift
        g_card = Card("Ghost lockstep")
        self.drift = DriftBar()
        self.drift_text = label("drift --", "kpi")
        self.ghost_text = label("", "muted")
        self.spark = Sparkline()
        g_card.add(self.drift_text)
        g_card.add(self.drift)
        g_card.add(self.spark)
        g_card.add(self.ghost_text)
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
        bri.addStretch(1)
        bri.addWidget(button("-", "small", lambda: self._bri(-10)))
        bri.addWidget(button("+", "small", lambda: self._bri(10)))
        h_card.body.addLayout(bri)
        grid.addWidget(h_card, 1, 0)

        # offset + quality
        o_card = Card("Timing")
        self.offset_lbl = label("+1.50 s", "kpi")
        o_card.add_row(label("Ghost runs ahead by", "muted"), self.offset_lbl, stretch_last=True)
        row = QHBoxLayout()
        for d, t in ((-0.25, "-0.25"), (-0.05, "-0.05"), (0.05, "+0.05"), (0.25, "+0.25")):
            row.addWidget(button(t, "small", lambda _=False, dd=d: self._offset(dd)))
        row.addStretch(1)
        o_card.body.addLayout(row)
        o_card.add(label("Lights late? increase. Lights early? decrease. Fine-tune on a hard cut.", "hint", wrap=True))
        self.quality = label("Sync quality: measuring...", "muted", wrap=True)
        o_card.add(self.quality)
        grid.addWidget(o_card, 1, 1)
        self.lay.addStretch(1)

    def _set_intensity(self, level: str) -> None:
        run_async(lambda: self.ctx.daemon.action("set", {"intensity": level}),
                  on_error=lambda e: self.ctx.toast(e, "bad"))

    def _bri(self, step: int) -> None:
        run_async(lambda: self.ctx.daemon.action("set", {"brightness_step": step}),
                  on_error=lambda e: self.ctx.toast("Brightness: %s" % e, "warn"))

    def _offset(self, delta: float) -> None:
        run_async(lambda: self.ctx.daemon.action("set", {"offset_delta": delta}),
                  on_error=lambda e: self.ctx.toast(e, "bad"))

    def refresh(self, st: dict) -> None:
        setup = st.get("setup_required") or []
        self.banner.setVisible(bool(setup))
        if setup:
            self.banner.text.setText("Setup needed: " + setup[0])
        state = st.get("state", "idle")
        f, g, e = st.get("follow", {}), st.get("ghost", {}), st.get("engine", {})
        if not st.get("enabled"):
            key, txt, sub = "disabled", "Sync disabled", "Turn the switch on to follow the TV again."
        elif setup:
            key, txt, sub = "setup", "Not set up yet", "Pick the Jellyfin player to follow, the ghost display and Hue Sync."
        elif state == "syncing":
            key, txt, sub = "syncing", "Syncing", "Hue Sync is streaming the ghost to your entertainment area."
        elif state == "ghosting":
            key = "ghosting"
            txt = "Ghost playing"
            sub = ("Waiting for Hue Sync to start streaming..." if e.get("connected")
                   else "Hue Sync app not reachable - lights are not driven. " + (e.get("error") or ""))
        else:
            key = "idle"
            txt = "Idle"
            dev = f.get("device") or f.get("device_name_contains") or f.get("device_id") or "the TV"
            sub = ("Waiting for %s to play something." % dev if f.get("seen")
                   else "%s is not connected to Jellyfin right now." % dev)
            if not st.get("jellyfin", {}).get("ok"):
                sub = "Jellyfin unreachable: %s" % (st.get("jellyfin", {}).get("error") or "?")
                key = "error"
        col = theme.STATE_COLORS[key]
        self.hero.setStyleSheet("QFrame#card{background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 %s, stop:1 %s);"
                                "border:none;}" % (col, theme.CARD))
        self.hero_state.setText(txt)
        self.hero_sub.setText(sub)

        if f.get("playing"):
            self.np_title.setText(f.get("item") or "?")
            flags = " (paused)" if f.get("paused") else " (buffering)" if f.get("buffering") else ""
            self.np_device.setText("%s%s" % (f.get("device") or "", flags))
            pos, rt = f.get("position_s") or 0.0, f.get("runtime_s")
            self.np_bar.setValue(int(1000 * pos / rt) if rt else 0)
            self.np_time.setText("%s / %s" % (_fmt_time(pos), _fmt_time(rt)))
        else:
            self.np_title.setText("Nothing")
            self.np_device.setText(f.get("device") or "")
            self.np_bar.setValue(0)
            self.np_time.setText("")

        d = st.get("drift_s")
        self.drift.set_value(d)
        self.spark.push(d if g.get("alive") else None)
        self.drift_text.setText("drift %+.2f s" % d if d is not None else "drift --")
        if g.get("alive"):
            self.ghost_text.setText("ghost at %s  speed %.3f  seeks %d  nudges %d" % (
                _fmt_time(g.get("position_s")), g.get("speed") or 1.0, g.get("seeks") or 0, g.get("nudges") or 0))
        else:
            self.ghost_text.setText("no ghost running")

        if e.get("connected"):
            hs_state = e.get("state") or "connected"
            set_pill(self.hs_pill, hs_state.replace("_", " "),
                     theme.GOOD if e.get("syncing") else theme.INFO if hs_state == "bridge_connected" else theme.WARN)
            self.hs_text.setText("mode %s  intensity %s  brightness %s" % (e.get("mode"), e.get("intensity"), e.get("bri")))
        else:
            set_pill(self.hs_pill, "not reachable", theme.BAD if e.get("name") == "huesync" else theme.STATE_COLORS["idle"])
            self.hs_text.setText(e.get("error") or ("engine: %s" % e.get("name")))
        self.intensity.set_value(st.get("intensity"))
        self.offset_lbl.setText("%+.2f s" % (st.get("offset_s") or 0.0))
        q = st.get("drift_last_minute")
        if q:
            self.quality.setText("Last minute: mean |drift| %.2f s, p95 %.2f s, max %.2f s, %d seek(s)" % (
                q["mean_abs"], q["p95_abs"], q["max_abs"], q["seeks"]))


# ============================================================================================
class SyncPage(Page):
    key, title, subtitle = "sync", "Sync", "How tightly the ghost follows the TV"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        c = Card("Offset - how far ahead the ghost runs")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(-200, 500)      # hundredths of a second
        self.slider.setSingleStep(5)
        self.spin = QDoubleSpinBox()
        self.spin.setRange(-2.0, 5.0)
        self.spin.setSingleStep(0.05)
        self.spin.setDecimals(2)
        self.spin.setSuffix(" s")
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
        c.add(label("The ghost intentionally plays this far ahead of the TV to cancel the capture -> bridge -> "
                    "lamp latency (and the TV's own display lag). Watch a hard cut: if the lights change late, "
                    "increase it; if early, decrease. Applies instantly.", "hint", wrap=True))
        self.lay.addWidget(c)

        c2 = Card("Hue Sync intensity for movies")
        self.intensity = Segmented(INTENSITY_OPTIONS, theme.INTENSITY_COLORS)
        self.intensity.changed.connect(lambda v: run_async(lambda: ctx.daemon.action("set", {"intensity": v}),
                                                           on_error=lambda e: ctx.toast(e, "bad")))
        c2.add(self.intensity)
        c2.add(label("Applied to the Hue Sync session whenever a movie starts (and live while syncing).", "hint", wrap=True))
        self.lay.addWidget(c2)

        adv = QGroupBox("Advanced lockstep tuning")
        g = QGridLayout(adv)
        g.setHorizontalSpacing(12)
        self.fields: dict[str, QDoubleSpinBox] = {}
        specs = [
            ("sync.seek_threshold_s", "Hard-seek beyond (s)", 0.3, 5.0, 0.1, "Drift larger than this jumps the ghost instead of nudging speed."),
            ("sync.deadband_s", "Deadband (s)", 0.0, 0.5, 0.01, "Drift inside this is ignored."),
            ("sync.converge_s", "Converge time (s)", 1.0, 30.0, 0.5, "How quickly small drift is removed by bending speed."),
            ("sync.max_speed_delta", "Max speed change", 0.01, 0.15, 0.01, "+/- playback speed used for nudging (0.04 = 4%)."),
            ("sync.jitter_tolerance_s", "Seek detection (s)", 0.5, 5.0, 0.1, "Position jumps larger than this count as a real seek."),
            ("sync.idle_stop_delay_s", "Stop after idle (s)", 2.0, 120.0, 1.0, "Lights stop this long after the TV stops."),
            ("sync.seek_cooldown_s", "Seek cooldown (s)", 1.0, 15.0, 0.5, "Minimum time between drift-seeks."),
            ("jellyfin.poll_interval_s", "Jellyfin poll (s)", 0.25, 5.0, 0.25, "How often the TV's position is read from Jellyfin."),
        ]
        for i, (key, name, lo, hi, step, tip) in enumerate(specs):
            lb = QLabel(name)
            lb.setToolTip(tip)
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi)
            sp.setSingleStep(step)
            sp.setDecimals(2)
            sp.setToolTip(tip)
            self.fields[key] = sp
            g.addWidget(lb, i // 2, (i % 2) * 2)
            g.addWidget(sp, i // 2, (i % 2) * 2 + 1)
        self.lay.addWidget(adv)

        st_card = Card("Learned TV buffering")
        self.stalls = label("", "muted", wrap=True)
        st_card.add(self.stalls)
        st_card.add(label("After a seek / start / resume the TV shows a still frame while it buffers. Hue Ghost "
                          "learns how long, holds the ghost for that long, then resumes exactly on target.", "hint", wrap=True))
        st_card.add_row(button("Reset learned values", "ghost", self._reset_stalls), stretch_last=True)
        self.lay.addWidget(st_card)
        self.lay.addWidget(button("Save advanced settings", "primary", self._save_adv))
        self.lay.addStretch(1)
        self._loaded = False

    def on_show(self) -> None:
        cfg = self.ctx.daemon.cfg
        for key, sp in self.fields.items():
            sp.blockSignals(True)
            sp.setValue(float(cfg.get(key)))
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
        self.stalls.setText("  ".join("%s: %.1f s" % (k, v) for k, v in est.items()) or "-")
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
        self.save(partial, "Lockstep settings saved")

    def _reset_stalls(self) -> None:
        self.ctx.api.reset_stalls({}, {})
        self.ctx.toast("Learned buffering reset to defaults", "ok")


# ============================================================================================
class PlayerPage(Page):
    key, title, subtitle = "player", "Player", "Which Jellyfin client the ghost follows"

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
        c.add_row(label("Server URL"), self.url)
        c.add_row(label("API key"), self.key, show)
        self.server_status = label("", "muted", wrap=True)
        c.add_row(button("Test connection and list players", "primary", self._test), self.server_status, stretch_last=False)
        self.lay.addWidget(c)

        c2 = Card("Player to follow")
        c2.add(label("Start playing something on the TV, then pick it here. Matching by exact device is most "
                     "reliable; matching by name works when the device id changes (e.g. after reinstalling the app).",
                     "hint", wrap=True))
        self.list = QListWidget()
        self.list.setMinimumHeight(160)
        self.list.itemSelectionChanged.connect(self._picked)
        c2.add(self.list)
        self.by_name = QLineEdit()
        self.by_name.setPlaceholderText("device name contains... e.g. Apple TV")
        self.user = QLineEdit()
        self.user.setPlaceholderText("optional: only this Jellyfin user")
        c2.add_row(label("Or match by name"), self.by_name)
        c2.add_row(label("User filter"), self.user)
        self.picked = label("", "muted", wrap=True)
        c2.add(self.picked)
        self.lay.addWidget(c2)
        self.lay.addWidget(button("Save player", "primary", self._save))
        self.lay.addStretch(1)
        self._device_id = ""

    def on_show(self) -> None:
        cfg = self.ctx.daemon.cfg
        self.url.setText(cfg.get("jellyfin.url") or "")
        self.key.setText(cfg.get("jellyfin.api_key") or "")
        self._device_id = cfg.get("jellyfin.follow.device_id") or ""
        self.by_name.setText(cfg.get("jellyfin.follow.device_name_contains") or "")
        self.user.setText(cfg.get("jellyfin.follow.user") or "")
        self._show_picked()
        if self.url.text() and self.key.text() and self.list.count() == 0:
            self._test()

    def _show_picked(self) -> None:
        if self._device_id:
            self.picked.setText("Following device id %s (%s)" % (self._device_id, self.by_name.text() or "?"))
        elif self.by_name.text():
            self.picked.setText("Following any device whose name contains '%s'" % self.by_name.text())
        else:
            self.picked.setText("Nothing selected yet")

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
                if s["device_id"] == self._device_id:
                    it.setSelected(True)
            if not res["sessions"]:
                self.server_status.setText(self.server_status.text() + " - play something on the TV to see it")

        run_async(lambda: self.ctx.api.sessions({}, {"url": url, "api_key": key}), done,
                  lambda e: self.server_status.setText(e))

    def _picked(self) -> None:
        items = self.list.selectedItems()
        if not items:
            return
        s = items[0].data(Qt.UserRole)
        self._device_id = s["device_id"] or ""
        self.by_name.setText(s["device_name"] or "")
        self._show_picked()

    def _save(self) -> None:
        partial = {"jellyfin": {"url": self.url.text().strip().rstrip("/"), "api_key": self.key.text().strip(),
                                "follow": {"device_id": self._device_id if self.list.selectedItems() else "",
                                           "device_name_contains": self.by_name.text().strip(),
                                           "user": self.user.text().strip()}}}
        if not self.list.selectedItems():
            partial["jellyfin"]["follow"]["device_id"] = ""
        self.save(partial, "Player saved")
        self._show_picked()


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
        self.displays.setMinimumHeight(100)
        c.add(self.displays)
        c.add_row(button("Refresh", "ghost", self._load_displays),
                  button("Show test pattern for 8 s", None, self._test), stretch_last=True)
        self.fullscreen = ToggleSwitch()
        c.add_row(label("Fullscreen on that display"), self.fullscreen, stretch_last=True)
        self.geometry = QLineEdit()
        self.geometry.setPlaceholderText("28%x28%-40-40  (windowed size/position)")
        c.add_row(label("Window geometry (if not fullscreen)"), self.geometry)
        self.lay.addWidget(c)

        c2 = Card("mpv player")
        self.mpv_path = QLineEdit()
        self.mpv_path.setPlaceholderText("mpv (auto-detect)")
        c2.add_row(label("mpv path"), self.mpv_path, button("Browse", "ghost", self._browse))
        self.mpv_status = label("", "muted", wrap=True)
        c2.add(self.mpv_status)
        self.hwdec = QComboBox()
        self.hwdec.addItems(["auto", "auto-safe", "d3d11va", "dxva2", "nvdec", "no"])
        c2.add_row(label("Hardware decoding"), self.hwdec, stretch_last=True)
        self.lay.addWidget(c2)
        self.lay.addWidget(button("Save display settings", "primary", self._save))
        self.lay.addStretch(1)

    def on_show(self) -> None:
        cfg = self.ctx.daemon.cfg
        self.fullscreen.setChecked(bool(cfg.get("ghost.fullscreen", True)))
        self.geometry.setText(cfg.get("ghost.geometry") or "")
        self.mpv_path.setText("" if (cfg.get("ghost.mpv_path") in (None, "", "mpv")) else cfg.get("ghost.mpv_path"))
        self.hwdec.setCurrentText(cfg.get("ghost.hwdec") or "auto")
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
        c.add_row(self.probe, button("Re-check", "ghost", self._probe), stretch_last=True)
        c.add(self.app_status)
        self.lay.addWidget(c)

        c2 = Card("Checklist (in the Hue Sync app, once)")
        for t in ("Settings > Allow public control: ON  (this is how Hue Ghost starts and stops sync)",
                  "Display: pick the ghost display (the virtual display / dummy plug)",
                  "Select the entertainment area of the room where the TV is",
                  "Start syncing when Hue Sync launches: OFF  (it would sync your desktop)"):
            c2.add(label("- " + t, "muted", wrap=True))
        self.lay.addWidget(c2)

        c3 = Card("Engine")
        self.engine = QComboBox()
        self.engine.addItem("Hue Sync app (recommended)", "huesync")
        self.engine.addItem("HTTP hook (GET url/start, url/stop)", "httphook")
        self.engine.addItem("None - I start sync myself", "none")
        c3.add_row(label("Light engine"), self.engine, stretch_last=True)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        c3.add_row(label("Public control port"), self.port, stretch_last=True)
        self.mode = QComboBox()
        self.mode.addItems(["video", "games", "music"])
        c3.add_row(label("Hue Sync mode for movies"), self.mode, stretch_last=True)
        self.launch = ToggleSwitch()
        c3.add_row(label("Start Hue Sync if it is not running"), self.launch, stretch_last=True)
        self.required = ToggleSwitch()
        c3.add_row(label("Only run the ghost when Hue Sync is reachable"), self.required, stretch_last=True)
        self.hook_url = QLineEdit()
        self.hook_url.setPlaceholderText("http://127.0.0.1:8989")
        c3.add_row(label("HTTP hook URL"), self.hook_url)
        self.lay.addWidget(c3)
        self.lay.addWidget(button("Save Hue Sync settings", "primary", self._save))
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
        c.add_row(label("Allow control from the LAN (Home Assistant, phone)"), self.expose, stretch_last=True)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        c.add_row(label("Port"), self.port, stretch_last=True)
        self.token = QLineEdit()
        self.token.setPlaceholderText("token (required when exposed)")
        c.add_row(label("Token"), self.token, button("Generate", "ghost", self._gen), button("Copy", "ghost", self._copy))
        self.addr = label("", "muted", wrap=True)
        c.add(self.addr)
        self.lay.addWidget(c)
        c2 = Card("Hue Synco integration (recommended)")
        c2.add(label("In Home Assistant: Settings > Devices & services > Hue Synco > Configure > enter this PC's "
                     "address, port and token. You get a Movie mode switch, a state sensor, an intensity select and "
                     "a sync-offset number - and movie mode hands the entertainment area over from music sync "
                     "automatically.", "muted", wrap=True))
        c2.add(label("Without Hue Synco: POST /on, /off, /set {\"offset_delta\": 0.25}; GET /status. "
                     "Header: Authorization: Bearer <token>.", "hint", wrap=True))
        self.lay.addWidget(c2)
        self.lay.addWidget(button("Save network settings", "primary", self._save))
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
        self.addr.setText("This PC: %s  ->  http://%s:%d" % (socket.gethostname(), ip, self.port.value()))

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
class SettingsPage(Page):
    key, title, subtitle = "settings", "Settings", "App behaviour"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        c = Card("Startup")
        self.autostart = ToggleSwitch()
        self.autostart.toggled.connect(self._autostart)
        c.add_row(label("Start Hue Ghost when I sign in (minimised to the tray)"), self.autostart, stretch_last=True)
        self.lay.addWidget(c)
        c2 = Card("Diagnostics")
        self.level = QComboBox()
        self.level.addItems(["INFO", "DEBUG", "WARNING"])
        self.level.currentTextChanged.connect(lambda v: self.save({"log_level": v}, "Log level: " + v))
        c2.add_row(label("Log level"), self.level, stretch_last=True)
        c2.add_row(button("Open log file", "ghost", lambda: self._open("log")),
                   button("Open config folder", "ghost", lambda: self._open("folder")), stretch_last=True)
        self.paths = label("", "hint", wrap=True)
        c2.add(self.paths)
        self.lay.addWidget(c2)
        c3 = Card("About")
        c3.add(label("Hue Ghost - a software Hue Sync Box for Jellyfin. MIT licensed, not affiliated with Signify "
                     "or Jellyfin.", "muted", wrap=True))
        c3.add(label("https://github.com/engabd11/HueGhost", "muted"))
        self.version = label("", "muted")
        c3.add(self.version)
        self.lay.addWidget(c3)
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
        self.version.setText("version %s%s" % (info["version"], "  (installed build)" if info["frozen"] else "  (running from source)"))

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
class LogPage(Page):
    key, title, subtitle = "log", "Log", "What the daemon is doing (newest at the bottom)"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMinimumHeight(480)
        self.text.setStyleSheet("QPlainTextEdit{font-family: Consolas, monospace; font-size: 12px;}")
        self.lay.addWidget(self.text)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._load)
        self._last = None

    def on_show(self) -> None:
        self._load()
        self.timer.start()

    def hideEvent(self, e) -> None:  # noqa: N802
        self.timer.stop()
        super().hideEvent(e)

    def _load(self) -> None:
        res = self.ctx.api.log_tail({}, {"lines": "300"})
        txt = "\n".join(res["lines"])
        if txt != self._last:
            self._last = txt
            self.text.setPlainText(txt)
            self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())


PAGES = [HomePage, SyncPage, PlayerPage, DisplayPage, HueSyncPage, HomeAssistantPage, SettingsPage, LogPage]
NAV_ICONS = {"home": "⌂", "sync": "◷", "player": "▶", "display": "▣", "huesync": "✺",
             "ha": "⌬", "settings": "✱", "log": "≡"}
