"""hue-ghost command line."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import secrets
import socket
import sys
import time
import urllib.request

from . import __author__, __version__
from .config import Config, INTENSITIES, MODES, app_data_dir, log_path, resolve_config_path
from .jellyfin import JellyfinClient, JellyfinError, session_label
from .winutil import (autostart_installed, find_mpv, hue_sync_info, install_autostart,
                      list_displays, tray_command, uninstall_autostart)

log = logging.getLogger("hue-ghost")


def setup_logging(level: str, to_file: bool = True) -> None:
    lvl = getattr(logging, str(level or "INFO").upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger("hue-ghost")
    root.setLevel(lvl)
    root.handlers[:] = []
    if sys.stdout is not None:            # absent in a --windowed PyInstaller build
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        root.addHandler(ch)
    if to_file:
        try:
            p = log_path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(p, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:
            pass


# -- helpers -------------------------------------------------------------------------
def _control_url(cfg: Config) -> str:
    bind = cfg.get("control.bind", "127.0.0.1")
    host = "127.0.0.1" if bind in ("0.0.0.0", "", "::") else bind
    return "http://%s:%d" % (host, int(cfg.get("control.port", 8787)))


def _control_call(cfg: Config, path: str, payload: dict | None = None) -> dict:
    url = _control_url(cfg) + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    tok = cfg.get("control.token", "")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


def _ask(prompt: str, default: str = "") -> str:
    suffix = " [%s]" % default if default else ""
    try:
        v = input("%s%s: " % (prompt, suffix)).strip()
    except EOFError:
        return default
    return v or default


def _ask_yn(prompt: str, default: bool = True) -> bool:
    v = _ask(prompt + (" (Y/n)" if default else " (y/N)"), "")
    if not v:
        return default
    return v.lower().startswith("y")


def _probe_ws(host: str, port: int, timeout: float = 2.0) -> tuple[bool, dict | None, str]:
    from .wsclient import WebSocket, WebSocketError
    try:
        ws = WebSocket(host, port, "/", timeout=timeout).connect()
    except (OSError, WebSocketError) as e:
        return False, None, str(e)
    try:
        ws.settimeout(timeout)
        text = ws.recv_text()
        msg = json.loads(text)
        return True, (msg.get("data") if isinstance(msg, dict) else None), ws.server_header
    except Exception as e:
        return True, None, "connected but no state event (%s)" % e
    finally:
        ws.close()


# -- commands ------------------------------------------------------------------------
def cmd_run(args) -> int:
    cfg = Config.load(args.config)
    setup_logging(cfg.get("log_level", "INFO"))
    if cfg.migrated:
        log.info("loaded v1 config from %s (migrated in memory; run 'hue-ghost setup' to rewrite)", cfg.path)
    probs = cfg.problems()
    for p in probs:
        log.warning("config: %s", p)
    from .daemon import Daemon
    d = Daemon(cfg)
    if probs:
        log.warning("setup required - run 'hue-ghost gui' (or 'hue-ghost setup')")
    try:
        d.run()
    except KeyboardInterrupt:
        d.stop()
        log.info("stopped by user")
    _relaunch_if_requested(d)
    return 0


def cmd_tray(args) -> int:
    cfg = Config.load(args.config)
    setup_logging(cfg.get("log_level", "INFO"))
    try:
        from .tray import run_tray
    except ImportError as e:
        log.error("tray needs the optional dependencies: pip install \"hue-ghost[tray]\" (%s)", e)
        return 2
    return run_tray(cfg)


def cmd_status(args) -> int:
    cfg = Config.load(args.config)
    try:
        st = _control_call(cfg, "/status")
    except Exception as e:
        print("hue-ghost is not running (or control API unreachable at %s): %s" % (_control_url(cfg), e))
        return 1
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    f, g, e = st["follow"], st["ghost"], st["engine"]
    print("hue-ghost %s  state=%s  enabled=%s" % (st["version"], st["state"], st["enabled"]))
    print("  jellyfin : %s (%s)" % (st["jellyfin"]["url"], "ok" if st["jellyfin"]["ok"] else st["jellyfin"]["error"]))
    if f["playing"]:
        print("  following: %s  '%s'  at %.1fs%s  (%d reports)" % (
            f["device"], f["item"], f["position_s"] or 0, " PAUSED" if f["paused"] else "", f["reports"]))
    else:
        print("  following: %s  (%s)" % (f["device"] or f["device_id"] or f["device_name_contains"],
                                         "idle" if f["seen"] else "not connected"))
    if g["alive"]:
        print("  ghost    : at %.1fs  speed %.3f  drift %s s  seeks %d" % (
            g["position_s"] or 0, g["speed"] or 1, st["drift_s"], g["seeks"]))
    print("  engine   : %s  connected=%s  state=%s  mode=%s  intensity=%s  bri=%s%s" % (
        e["name"], e["connected"], e["state"], e["mode"], e["intensity"], e["bri"],
        ("  error=" + e["error"]) if e.get("error") else ""))
    print("  offset   : %+.2fs   wanted: mode %s, intensity %s, audio %s" % (
        st["offset_s"], st.get("mode"), st["intensity"],
        {True: "on", False: "off"}.get(st.get("use_audio"), "app's own")))
    dl = st.get("drift_last_minute")
    if dl:
        print("  last min : mean|drift| %.3fs  p95 %.3fs  max %.3fs  seeks %d" % (
            dl["mean_abs"], dl["p95_abs"], dl["max_abs"], dl["seeks"]))
    return 0


def _simple_action(path: str):
    def run(args) -> int:
        cfg = Config.load(args.config)
        try:
            print(json.dumps(_control_call(cfg, path, {})))
            return 0
        except Exception as e:
            print("control API unreachable at %s: %s" % (_control_url(cfg), e))
            return 1
    return run


def cmd_set(args) -> int:
    cfg = Config.load(args.config)
    payload = {}
    if args.offset is not None:
        payload["offset_s"] = args.offset
    if args.offset_delta is not None:
        payload["offset_delta"] = args.offset_delta
    if args.intensity:
        payload["intensity"] = args.intensity
    if args.mode:
        payload["mode"] = args.mode
    if args.use_audio:
        payload["use_audio"] = args.use_audio
    if args.brightness_step is not None:
        payload["brightness_step"] = args.brightness_step
    if args.brightness is not None:
        payload["brightness"] = args.brightness
    if args.enable_source or args.disable_source:
        payload["binding"] = {"key": args.enable_source or args.disable_source,
                              "enabled": bool(args.enable_source)}
    if not payload:
        print("nothing to set (see --help)")
        return 2
    try:
        print(json.dumps(_control_call(cfg, "/set", payload)))
        return 0
    except Exception as e:
        print("control API unreachable at %s: %s" % (_control_url(cfg), e))
        return 1


def cmd_doctor(args) -> int:
    cfg = Config.load(args.config)
    ok_all = True

    def row(status: str, what: str, detail: str = "") -> None:
        nonlocal ok_all
        if status == "FAIL":
            ok_all = False
        print("  [%s] %-22s %s" % (status, what, detail))

    print("hue-ghost %s doctor  (built by %s)" % (__version__, __author__))
    print("config: %s%s" % (cfg.path, "" if cfg.exists() else "  (missing - run: hue-ghost setup)"))
    if cfg.migrated:
        row("WARN", "config format", "v1 flat config; run 'hue-ghost setup' to rewrite")
    for p in cfg.problems():
        row("FAIL", "config", p)

    # Jellyfin
    jf = JellyfinClient(cfg.get("jellyfin.url"), cfg.get("jellyfin.api_key"))
    try:
        info = jf.public_info()
        row("PASS", "jellyfin server", "%s %s at %s" % (info.get("ServerName"), info.get("Version"), cfg.get("jellyfin.url")))
    except JellyfinError as e:
        row("FAIL", "jellyfin server", str(e))
        info = None
    if info is not None:
        try:
            sessions, server_epoch, local = jf.sessions()
            row("PASS", "jellyfin api key", "%d session(s); server clock offset %+.1fs" % (
                len(sessions), (server_epoch - local) if server_epoch else 0.0))
            from .watcher import SessionMatcher
            f = cfg.get("jellyfin.follow", {}) or {}
            m = SessionMatcher(f.get("device_id", ""), f.get("device_name_contains", ""), f.get("user", ""))
            picked = m.pick(sessions)
            if picked:
                np = picked.get("NowPlayingItem")
                row("PASS", "followed client", "%s%s" % (session_label(picked),
                    ("  playing '%s'" % np.get("Name")) if np else "  (idle)"))
            else:
                row("WARN", "followed client", "not connected right now (%s)" % (
                    f.get("device_id") or f.get("device_name_contains") or "nothing configured"))
                for s in sessions[:8]:
                    print("         seen: %s  DeviceId=%s" % (session_label(s), s.get("DeviceId")))
        except JellyfinError as e:
            row("FAIL", "jellyfin api key", str(e))

    # mpv
    mpv = find_mpv(cfg.get("ghost.mpv_path", "mpv"))
    if mpv:
        row("PASS", "mpv", mpv)
    else:
        row("FAIL", "mpv", "not found - install from https://mpv.io (or set ghost.mpv_path)")

    # displays
    disps = list_displays()
    if disps:
        want = cfg.get("ghost.screen_name") or ""
        idx = cfg.get("ghost.screen_index")
        for d in disps:
            mark = ""
            if (want and d.name == want) or (idx is not None and idx != "" and int(idx) == d.index):
                mark = "  <- ghost"
            print("         display %d: %s%s" % (d.index, d.label(), mark))
        if want and not any(d.name == want for d in disps):
            row("FAIL", "ghost display", "%s not present" % want)
        elif not want and (idx is None or idx == ""):
            row("WARN", "ghost display", "none chosen: ghost goes fullscreen on the current display")
        else:
            row("PASS", "ghost display", want or ("index %s" % idx))
    else:
        row("WARN", "displays", "enumeration unavailable on this platform")

    # engine
    et = cfg.get("engine.type")
    if et == "huesync":
        hs = hue_sync_info()
        host, port = cfg.get("engine.huesync.host"), int(cfg.get("engine.huesync.port"))
        if hs["installed"]:
            row("PASS", "hue sync app", hs["exe"])
        else:
            row("WARN", "hue sync app", "install dir not found (still fine if it runs from elsewhere)")
        if hs["public_control_enabled"] is False:
            row("FAIL", "hue sync public control", "toggle 'Allow public control' in Hue Sync > Settings")
        if hs["public_control_port"] and int(hs["public_control_port"]) != port:
            row("FAIL", "hue sync port", "app uses %s, config says %d" % (hs["public_control_port"], port))
        if hs["automatic_display"] is True and disps and len(disps) > 1:
            row("WARN", "hue sync display", "Automatic display is on; pick the ghost display in Hue Sync > Display")
        if hs["selected_area"]:
            row("PASS", "hue sync area", str(hs["selected_area"]))
        up, state, hdr = _probe_ws(host, port)
        if up and state:
            row("PASS", "hue sync websocket", "%s:%d  state=%s mode=%s intensity=%s bri=%s" % (
                host, port, state.get("state"), state.get("mode"), state.get("intensity"), state.get("bri")))
            if state.get("state") == "bridge_disconnected":
                row("FAIL", "hue bridge", "Hue Sync is not connected to a bridge")
        elif up:
            row("WARN", "hue sync websocket", hdr)
        else:
            row("FAIL", "hue sync websocket", "%s:%d unreachable (%s) - is Hue Sync running?" % (host, port, hdr))
    elif et == "httphook":
        row("PASS", "engine", "httphook -> " + str(cfg.get("engine.httphook.url")))
    else:
        row("WARN", "engine", "none: lights are not automated")

    # control API
    bind, port = cfg.get("control.bind"), int(cfg.get("control.port"))
    if bind not in ("127.0.0.1", "localhost") and not cfg.get("control.token"):
        row("WARN", "control api", "exposed on %s:%d without a token" % (bind, port))
    else:
        row("PASS", "control api", "%s:%d%s" % (bind, port, "  (token set)" if cfg.get("control.token") else ""))
    try:
        st = _control_call(cfg, "/health")
        row("PASS", "daemon", "running (v%s)" % st.get("version"))
    except Exception:
        row("WARN", "daemon", "not running")
    row("PASS" if autostart_installed() else "WARN", "autostart",
        "installed" if autostart_installed() else "not installed (hue-ghost install-autostart)")
    print("result:", "OK" if ok_all else "problems found")
    return 0 if ok_all else 1


def cmd_setup(args) -> int:
    cfg = Config.load(args.config)
    print("hue-ghost setup - answers are saved to %s\n" % cfg.path)

    # 1. Jellyfin
    while True:
        url = _ask("Jellyfin server URL", cfg.get("jellyfin.url"))
        try:
            info = JellyfinClient(url, "").public_info()
            print("  found %s (Jellyfin %s)" % (info.get("ServerName"), info.get("Version")))
            cfg.set("jellyfin.url", url.rstrip("/"))
            break
        except JellyfinError as e:
            print("  cannot reach it: %s" % e)
            if not _ask_yn("Try again?"):
                return 1
    while True:
        print("  API key: Jellyfin Dashboard > API Keys > + (name it hue-ghost)")
        key = _ask("Jellyfin API key", cfg.get("jellyfin.api_key"))
        jf = JellyfinClient(cfg.get("jellyfin.url"), key)
        try:
            sessions, _, _ = jf.sessions()
            cfg.set("jellyfin.api_key", key)
            break
        except JellyfinError as e:
            print("  key rejected: %s" % e)

    # 2. followed client
    print("\nWhich client should the ghost follow? Start playing something on it to see it here.")
    seen = [s for s in sessions if s.get("DeviceId")]
    for i, s in enumerate(seen, 1):
        np = s.get("NowPlayingItem")
        print("  %d) %s%s" % (i, session_label(s), ("  playing '%s'" % np.get("Name")) if np else ""))
    print("  0) type a device-name substring instead")
    choice = _ask("Pick", "0" if not seen else "1")
    if choice.isdigit() and 0 < int(choice) <= len(seen):
        s = seen[int(choice) - 1]
        cfg.set("jellyfin.follow.device_id", s.get("DeviceId"))
        cfg.set("jellyfin.follow.device_name_contains", s.get("DeviceName") or "")
    else:
        cfg.set("jellyfin.follow.device_id", "")
        cfg.set("jellyfin.follow.device_name_contains",
                _ask("Device name contains", cfg.get("jellyfin.follow.device_name_contains") or "Apple TV"))

    # 3. mpv
    mpv = find_mpv(cfg.get("ghost.mpv_path"))
    if mpv:
        print("\nmpv: %s" % mpv)
        cfg.set("ghost.mpv_path", mpv)
    else:
        print("\nmpv not found. Install it from https://mpv.io (Windows: 'winget install mpv' or scoop),")
        cfg.set("ghost.mpv_path", _ask("then enter the path to mpv.exe", "mpv"))

    # 4. display
    disps = list_displays()
    if disps:
        print("\nDisplays (the ghost goes fullscreen on one; Hue Sync must capture the same one):")
        for d in disps:
            print("  %d) %s" % (d.index, d.label()))
        print("  A virtual display (Virtual Display Driver) or an HDMI dummy plug keeps your real")
        print("  screen free. See docs/virtual-display.md.")
        default = next((str(d.index) for d in disps if not d.primary), str(disps[0].index))
        pick = _ask("Ghost display index", default)
        d = next((x for x in disps if str(x.index) == pick), disps[0])
        cfg.set("ghost.screen_name", d.name)
        cfg.set("ghost.screen_index", d.index)
        cfg.set("ghost.fullscreen", True)

    # 5. Hue Sync
    hs = hue_sync_info()
    print("\nHue Sync app: %s" % (hs["exe"] or "not found in the usual place"))
    if hs["public_control_enabled"] is False:
        print("  Enable Hue Sync > Settings > 'Allow public control' (this is how hue-ghost starts/stops sync).")
    if hs["public_control_port"]:
        cfg.set("engine.huesync.port", int(hs["public_control_port"]))
    if hs["selected_area"]:
        print("  Hue Sync currently targets entertainment area: %s" % hs["selected_area"])
    up, state, hdr = _probe_ws(cfg.get("engine.huesync.host"), int(cfg.get("engine.huesync.port")))
    print("  public control: %s" % ("reachable, state=%s" % (state or {}).get("state") if up else "unreachable (%s)" % hdr))
    cfg.set("engine.type", "huesync")
    if hs["exe"] and _ask_yn("Start Hue Sync automatically if it is not running?", False):
        cfg.set("engine.huesync.launch_exe", hs["exe"])
    mode = _ask("Hue Sync mode for movies (%s)" % "/".join(MODES), cfg.get("engine.huesync.mode"))
    cfg.set("engine.huesync.mode", mode if mode in MODES else "video")
    lvl = _ask("Intensity for movies (%s)" % "/".join(INTENSITIES), cfg.get("engine.huesync.intensity"))
    cfg.set("engine.huesync.intensity", lvl if lvl in INTENSITIES else "high")
    aud = _ask("Use audio for light effects (on/off/app = leave it to the Hue Sync app)",
               {True: "on", False: "off"}.get(cfg.get("engine.huesync.use_audio"), "app"))
    cfg.set("engine.huesync.use_audio", {"on": True, "off": False}.get((aud or "").strip().lower()))
    cfg.set("sync.offset_s", float(_ask("Sync offset in seconds (ghost runs ahead by this)", str(cfg.get("sync.offset_s")))))

    # 6. control API
    if _ask_yn("\nExpose the control API to your LAN for Home Assistant?", cfg.get("control.bind") == "0.0.0.0"):
        cfg.set("control.bind", "0.0.0.0")
        tok = cfg.get("control.token") or secrets.token_urlsafe(24)
        cfg.set("control.token", tok)
        print("  token (paste into Home Assistant): %s" % tok)
        print("  this PC: %s  port %d" % (socket.gethostbyname(socket.gethostname()), int(cfg.get("control.port"))))
    else:
        cfg.set("control.bind", "127.0.0.1")

    path = cfg.save()
    print("\nsaved %s" % path)
    if _ask_yn("Start hue-ghost at login (tray icon)?", True):
        try:
            print("  " + install_autostart(_tray_command()))
        except Exception as e:
            print("  could not install autostart: %s" % e)
    print("done. Run: hue-ghost doctor   then   hue-ghost run  (or hue-ghost tray)")
    return 0


def _tray_command() -> list[str]:
    return tray_command()


def _relaunch_if_requested(daemon) -> None:
    if getattr(daemon, "restart_requested", False):
        import subprocess
        cmd = tray_command() if getattr(sys, "frozen", False) or daemon.cfg.get("_mode") == "tray" \
            else [sys.executable, "-m", "hueghost", "run"]
        log.info("relaunching: %s", " ".join(cmd))
        time.sleep(1.0)
        subprocess.Popen(cmd, close_fds=True)


def cmd_gui(args) -> int:
    cfg = Config.load(args.config)
    setup_logging(cfg.get("log_level", "INFO"))
    try:
        from .gui.app import run_gui
    except ImportError as e:
        log.error("the desktop app needs PySide6: pip install \"hue-ghost[gui]\" (%s)", e)
        return 2
    return run_gui(cfg, minimized=bool(getattr(args, "minimized", False)))


def cmd_install_autostart(args) -> int:
    print(install_autostart(_tray_command()))
    return 0


def cmd_uninstall_autostart(args) -> int:
    print("removed" if uninstall_autostart() else "not installed")
    return 0


def cmd_test_mpv(args) -> int:
    cfg = Config.load(args.config)
    setup_logging("INFO", to_file=False)
    from .ghost import GhostPlayer
    from .lockstep import Pause, Resume, Seek, Speed
    video = os.path.abspath(args.file)
    print("launching", video)
    cfg.set("ghost.mpv_path", find_mpv(cfg.get("ghost.mpv_path")) or cfg.get("ghost.mpv_path"))
    cfg.set("ghost.fullscreen", False)     # plumbing test: small window is enough
    g = GhostPlayer.launch(cfg, video, "X-Test: 1", 5.0, "test")
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not g.has_position:
            if not g.alive():
                raise RuntimeError("mpv died (rc=%s)" % g.proc.poll())
            time.sleep(0.1)
        if not g.has_position:
            raise RuntimeError("no time-pos from mpv within 10 s")
        print("first time-pos %.2f after %.2fs" % (g.pos, time.monotonic() - g.started_mono))
        for a in (Speed(1.03), Seek(15.0), Pause(), Resume(), Speed(1.0)):
            time.sleep(1.0)
            g.apply([a], time.monotonic())
            print("  %-20s -> pos %.2f paused=%s speed=%.3f buffering=%s" % (a, g.pos or -1, g.paused, g.speed, g.buffering))
        time.sleep(1.0)
        print("PASS: IPC readback works (pos %.2f)" % (g.pos or -1))
        return 0
    except Exception as e:
        print("FAIL:", e)
        return 1
    finally:
        g.kill()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hue-ghost", description="software Hue Sync Box for Jellyfin")
    p.add_argument("--config", help="config file (default: ./config.json, repo config.json, or %s)"
                   % os.path.join(app_data_dir(), "config.json"))
    p.add_argument("--version", action="version", version="hue-ghost %s - built by %s" % (__version__, __author__))
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the daemon in the foreground (no window)").set_defaults(fn=cmd_run)
    s = sub.add_parser("gui", help="the desktop app (daemon + control panel + tray icon)")
    s.add_argument("--minimized", action="store_true", help="start in the tray without showing the window")
    s.set_defaults(fn=cmd_gui)
    sub.add_parser("tray", help="lightweight tray icon without the desktop app (pystray)").set_defaults(fn=cmd_tray)
    sub.add_parser("setup", help="interactive setup wizard").set_defaults(fn=cmd_setup)
    sub.add_parser("doctor", help="check Jellyfin, mpv, displays and Hue Sync").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("status", help="show the running daemon's state")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)
    sub.add_parser("on", help="enable sync").set_defaults(fn=_simple_action("/on"))
    sub.add_parser("off", help="disable sync (stops the ghost)").set_defaults(fn=_simple_action("/off"))
    sub.add_parser("toggle", help="toggle sync").set_defaults(fn=_simple_action("/toggle"))
    sub.add_parser("reload", help="re-read config.json in the running daemon").set_defaults(fn=_simple_action("/reload"))
    s = sub.add_parser("set", help="change live settings in the running daemon")
    s.add_argument("--offset", type=float, help="sync offset in seconds")
    s.add_argument("--offset-delta", type=float, help="add to the current offset (e.g. 0.25 or -0.25)")
    s.add_argument("--intensity", choices=INTENSITIES)
    s.add_argument("--mode", choices=MODES, help="what Hue Sync reacts to")
    s.add_argument("--use-audio", choices=("on", "off", "app"),
                   help="use audio for light effects in video/games mode ('app' = leave the Hue Sync app's own setting)")
    s.add_argument("--brightness-step", type=int, help="Hue Sync brightness step (signed)")
    s.add_argument("--brightness", type=int, help="Hue Sync brightness, 0-100")
    s.add_argument("--enable-source", metavar="ID", help="follow this source again (see `status --json`)")
    s.add_argument("--disable-source", metavar="ID", help="stop following this source")
    s.set_defaults(fn=cmd_set)
    s = sub.add_parser("test-mpv", help="check the mpv IPC plumbing with a local file")
    s.add_argument("file")
    s.set_defaults(fn=cmd_test_mpv)
    sub.add_parser("install-autostart", help="start the tray app at login").set_defaults(fn=cmd_install_autostart)
    sub.add_parser("uninstall-autostart").set_defaults(fn=cmd_uninstall_autostart)
    return p


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        # double-click / no args: the desktop app if available, else tray, else run
        try:
            import PySide6  # noqa: F401
            args.minimized = False
            return cmd_gui(args)
        except ImportError:
            pass
        try:
            import pystray  # noqa: F401
            return cmd_tray(args)
        except ImportError:
            return cmd_run(args)
    return args.fn(args)
