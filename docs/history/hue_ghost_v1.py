#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hue-ghost - screen-sync living-room Philips Hue lights with Jellyfin playing
on the Apple TV, using this PC as a "ghost" screen for Hue Sync.

How it works
  1. Polls the Jellyfin Sessions API for the Apple TV client.
  2. When the ATV plays a video, launches a muted, small, always-on-top mpv
     window playing the SAME file via Jellyfin's direct stream URL.
  3. Keeps the ghost in lockstep with the ATV: playback-speed nudges for
     small drift, hard seeks beyond a threshold, pause/resume propagation.
  4. Hue Sync (official app, or Huestacean via its local HTTP API) captures
     that window and streams to the living-room entertainment area.
No Hue Sync Box required. The ghost playback is a plain HTTP file read, so
it creates no Jellyfin session - no "continue watching" pollution.

stdlib only, Python 3.8+.

  python hue_ghost.py                    run the daemon (Ctrl+C stops)
  python hue_ghost.py --test-mpv FILE    sanity-check the mpv IPC plumbing
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_PATH = os.path.join(HERE, "hue_ghost.log")

DEFAULTS = {
    "server_url": "http://192.168.0.156:8096",
    "api_key": "",
    "enabled": True,              # master switch; toggle live via control API
    "control_port": 8787,         # local control API: /on /off /toggle /status /reload
    "watch_device": "Apple TV",   # case-insensitive substring over DeviceName + Client
    "watch_user": "",             # optional username substring filter
    "poll_interval_s": 1.0,
    "seek_threshold_s": 1.0,      # beyond this, hard-seek the ghost
    "nudge_threshold_s": 0.25,    # beyond this, bend playback speed
    "max_speed_delta": 0.04,      # +/- speed clamp used for nudging
    "sync_offset_s": 1.0,         # ghost bias: ghost plays this many seconds
                                  # ahead of the ATV-reported position, to
                                  # cancel pipeline latency (ghost decode +
                                  # Hue Sync capture + bridge). Tune to taste.
    "seek_cooldown_s": 3.0,
    "idle_stop_delay_s": 10.0,    # stop ghost N seconds after ATV stops
    "relaunch_cooldown_s": 5.0,   # same-item relaunch guard (crash loop)
    "relaunch_max_per_5min": 6,
    "mpv_path": "mpv",
    "ghost_geometry": "28%x28%-40-40",
    "ipc_pipe": "\\\\.\\pipe\\hue-ghost",
    "huestacean_url": "",         # e.g. http://127.0.0.1:8989 (GET /start, /stop)
    "log_level": "INFO",
}

log = logging.getLogger("hue-ghost")


def asc(s):
    """ASCII-safe string for console/file logs."""
    return (s or "").encode("ascii", "replace").decode("ascii")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, indent=2)
        log.info("created default config at %s - fill in api_key", CONFIG_PATH)
        return dict(DEFAULTS)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if v not in (None, "") or k in
                   ("api_key", "watch_user", "huestacean_url", "server_url")})
    return merged


def setup_logging(cfg):
    level = getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    log.setLevel(level)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.handlers[:] = [fh, ch]


class Jellyfin:
    def __init__(self, base, key):
        self.base = base.rstrip("/")
        self.key = key
        self._msid_cache = {}

    def _get(self, path, timeout=5):
        url = self.base + path
        req = urllib.request.Request(url, headers={
            "Authorization": 'MediaBrowser Token="%s"' % self.key,
            "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def sessions(self):
        return self._get("/Sessions")

    def media_source_id(self, item_id):
        if item_id not in self._msid_cache:
            try:
                data = self._get("/Items?ids=%s&fields=MediaSources" % item_id)
                items = data.get("Items") or []
                ms = (items[0].get("MediaSources") or [{}])[0].get("Id") if items else None
                self._msid_cache[item_id] = ms
            except Exception:
                return None
        return self._msid_cache[item_id]

    def stream_url(self, item_id):
        url = self.base + "/Videos/" + str(item_id) + "/stream?static=true"
        msid = self.media_source_id(item_id)
        if msid:
            url += "&MediaSourceId=" + str(msid)
        return url


def find_watch(sessions, needle, userf):
    """Return dict with the watched session's play state, or item=None when idle."""
    seen = False
    for s in sessions or []:
        label = ((s.get("DeviceName") or "") + " " + (s.get("Client") or "")).lower()
        if needle and needle not in label:
            continue
        if userf and userf not in (s.get("UserName") or "").lower():
            continue
        seen = True
        np = s.get("NowPlayingItem")
        ps = s.get("PlayState") or {}
        if not np or np.get("MediaType") != "Video":
            continue  # idle or audio
        ticks = ps.get("PositionTicks")
        if ticks is None:
            continue
        return {"seen": True, "item": np.get("Id"), "pos": ticks / 1e7,
                "paused": bool(ps.get("IsPaused")), "name": np.get("Name") or "?"}
    return {"seen": seen, "item": None, "pos": None, "paused": True, "name": None}


class Ghost:
    """One ghost mpv process + a position estimator driven from wall clock."""

    def __init__(self, proc, item_id, start_pos, pipe_path, errf=None):
        self.proc = proc
        self.item_id = item_id
        self.pipe_path = pipe_path
        self.errf = errf
        self.pos = float(start_pos)
        self.t = time.monotonic()
        self.speed = 1.0
        self.paused = False
        self.pipe = None
        self.last_seek = 0.0

    def alive(self):
        return self.proc.poll() is None

    def _ensure_pipe(self):
        if self.pipe is not None:
            return
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if not self.alive():
                raise RuntimeError("mpv exited before IPC pipe opened")
            try:
                self.pipe = open(self.pipe_path, "r+b", buffering=0)
                return
            except OSError:
                time.sleep(0.25)
        raise RuntimeError("could not open IPC pipe " + self.pipe_path)

    def send(self, *cmd):
        self._ensure_pipe()
        self.pipe.write((json.dumps({"command": list(cmd)}) + "\n").encode("utf-8"))

    def advance(self, now, frozen):
        if not self.paused and not frozen:
            self.pos += (now - self.t) * self.speed
        self.t = now

    def set_speed(self, s):
        if abs(s - self.speed) > 1e-6:
            self.send("set_property", "speed", round(s, 4))
            self.speed = s

    def seek(self, target, now):
        if abs(self.speed - 1.0) > 1e-6:
            self.send("set_property", "speed", 1.0)
            self.speed = 1.0
        self.send("seek", round(max(0.0, target), 3), "absolute", "exact")
        self.pos = float(target)
        self.t = now
        self.last_seek = now

    def set_pause(self, p):
        if p != self.paused:
            self.send("set_property", "pause", bool(p))
            self.paused = bool(p)

    def kill(self):
        if self.pipe is not None:
            try:
                self.pipe.close()
            except OSError:
                pass
            self.pipe = None
        if self.errf is not None:
            try:
                self.errf.close()
            except OSError:
                pass
            self.errf = None
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def ghost_video_args(cfg, title):
    args = [cfg["mpv_path"], "--no-config", "--no-border",
            "--mute=yes", "--volume=0", "--osd-level=0", "--sub-visibility=no",
            "--hwdec=auto", "--keep-open=no",
            "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
            "--demuxer-readahead-secs=5",
            # user-escapable: ESC/q close, m minimize, f toggle fullscreen
            # (ghost_input.conf); stray keys do nothing, cursor stays visible
            "--input-default-bindings=no",
            "--input-conf=" + os.path.join(HERE, "ghost_input.conf")]
    if cfg.get("ghost_fullscreen"):
        args += ["--fullscreen=yes"]
    else:
        args += ["--fullscreen=no", "--geometry=" + cfg["ghost_geometry"]]
    args += ["--input-ipc-server=" + cfg["ipc_pipe"], "--title=" + title]
    return args

def launch_ghost(cfg, jf, item_id, start_pos):
    args = ghost_video_args(cfg, "hue-ghost")
    args += [
        "--http-header-fields=Authorization: MediaBrowser Token=\"%s\"" % cfg["api_key"],
        "--start=%.3f" % max(0.0, start_pos),
        jf.stream_url(item_id),
    ]
    proc = subprocess.Popen(args, cwd=HERE,
                            stdout=subprocess.DEVNULL,
                            stderr=open(os.path.join(HERE, "mpv_ghost.err"), "ab"))
    g = Ghost(proc, item_id, start_pos, cfg["ipc_pipe"],
              errf=proc.stderr)
    return g


def _start_control(port, state, cfg):
    """Tiny local HTTP control API: /on /off /toggle /status /reload."""
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass

        def _serve(self):
            path = self.path.split("?", 1)[0]
            try:
                if path == "/on":
                    state["enabled"] = True
                    self._send(200, {"enabled": True})
                elif path == "/off":
                    state["enabled"] = False
                    self._send(200, {"enabled": False})
                elif path == "/toggle":
                    state["enabled"] = not state.get("enabled", False)
                    self._send(200, {"enabled": state["enabled"]})
                elif path == "/status":
                    self._send(200, dict(state))
                elif path == "/reload":
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        new_cfg = json.load(f)
                    cfg.clear()
                    cfg.update(new_cfg)
                    self._send(200, {"reloaded": True,
                                     "sync_offset_s": new_cfg.get("sync_offset_s")})
                else:
                    self._send(404, {"error": "unknown path",
                                     "paths": ["/on", "/off", "/toggle",
                                               "/status", "/reload"]})
            except Exception as e:
                self._send(500, {"error": str(e)})

        def do_GET(self):
            self._serve()

        def log_message(self, fmt, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def poke_huestacean(base, action):
    if not base:
        return
    try:
        urllib.request.urlopen(base.rstrip("/") + "/" + action, timeout=2)
        log.info("huestacean %s ok", action)
    except Exception as e:
        log.warning("huestacean %s failed: %s", action, asc(str(e)))


def run(cfg):
    jf = Jellyfin(cfg["server_url"], cfg["api_key"])
    needle = str(cfg["watch_device"]).strip().lower()
    userf = str(cfg.get("watch_user", "")).strip().lower()

    ghost = None
    ghost_last_item = None
    last_launch = 0.0
    launches = deque()
    idle_since = None
    atv_item, atv_pos, atv_anchor_t, atv_paused = None, 0.0, 0.0, True
    last_err_log = 0.0
    guard_logged = False
    state = {"enabled": bool(cfg.get("enabled", True))}
    cport = int(cfg.get("control_port") or 0)
    if cport:
        try:
            _start_control(cport, state, cfg)
            log.info("control server on http://127.0.0.1:%d "
                     "(/on /off /toggle /status /reload)", cport)
        except Exception as e:
            log.warning("control server unavailable: %s", asc(str(e)))

    poll = float(cfg["poll_interval_s"])
    log.info("watching Jellyfin %s for '%s' (poll %.1fs)", cfg["server_url"],
             cfg["watch_device"], poll)

    try:
        while True:
            now = time.monotonic()
            off = float(cfg.get("sync_offset_s", 0.0))

            if not state.get("enabled", True):
                if ghost is not None:
                    log.info("sync disabled -> stopping ghost")
                    ghost.kill()
                    ghost = None
                    ghost_last_item = None
                    idle_since = None
                    poke_huestacean(cfg.get("huestacean_url", ""), "stop")
                state.update({"running": False, "item": None, "item_name": None,
                              "ghost_pos": None, "paused": None,
                              "offset_s": float(cfg.get("sync_offset_s", 0.0))})
                time.sleep(0.5)
                continue

            try:
                sessions = jf.sessions()
                last_err_log = 0.0
            except Exception as e:
                if now - last_err_log > 30:
                    log.warning("Jellyfin unreachable (%s) - keeping ghost as-is",
                                asc(str(e)))
                    last_err_log = now
                time.sleep(poll)
                continue

            w = find_watch(sessions, needle, userf)
            state.update({"enabled": True, "running": ghost is not None,
                          "item": atv_item, "item_name": w.get("name"),
                          "atv_pos": atv_pos, "paused": atv_paused,
                          "ghost_pos": (ghost.pos if ghost else None),
                          "offset_s": off})

            if ghost is not None and not ghost.alive():
                rc = ghost.proc.poll()
                log.info("ghost mpv exited (rc=%s)", rc)
                ghost = None
                if rc == 0:
                    state["enabled"] = False
                    idle_since = None
                    poke_huestacean(cfg.get("huestacean_url", ""), "stop")
                    log.info("ghost closed by user -> sync disabled "
                             "(sync-on.bat or /on to re-enable)")
                    continue  # do not relaunch in the same iteration

            if w["item"] is not None and w["pos"] is not None:
                idle_since = None
                was_paused = atv_paused
                atv_item, atv_pos, atv_anchor_t, atv_paused = (
                    w["item"], w["pos"], now, w["paused"])

                if ghost is not None and ghost.item_id != atv_item:
                    log.info("ATV switched to '%s' -> relaunching ghost",
                             asc(w["name"]))
                    ghost.kill()
                    ghost = None

                if ghost is None:
                    same_item = (atv_item == ghost_last_item)
                    launches = deque(t for t in launches if now - t < 300)
                    cooldown_ok = (not same_item) or (now - last_launch
                                                      >= cfg["relaunch_cooldown_s"])
                    rate_ok = len(launches) < int(cfg["relaunch_max_per_5min"])
                    if not rate_ok:
                        if not guard_logged:
                            log.error("launch rate guard hit (%d in 5min) - not "
                                      "relaunching; check mpv/logs",
                                      int(cfg["relaunch_max_per_5min"]))
                            guard_logged = True
                    elif cooldown_ok:
                        atv_est = atv_pos + off
                        log.info("ATV playing '%s' -> ghost at %.1fs (offset %+.1fs)",
                                 asc(w["name"]), atv_est, off)
                        try:
                            ghost = launch_ghost(cfg, jf, atv_item, atv_est)
                            ghost_last_item = atv_item
                            last_launch = now
                            launches.append(now)
                            guard_logged = False
                            poke_huestacean(cfg.get("huestacean_url", ""), "start")
                        except Exception as e:
                            log.error("ghost launch failed: %s", asc(str(e)))
                            last_launch = now
                    time.sleep(poll)
                    continue

                # both alive -> lockstep tick
                atv_est = atv_pos + (0.0 if atv_paused else (now - atv_anchor_t))
                ghost.advance(now, frozen=atv_paused)

                if was_paused and not atv_paused:
                    ghost.set_pause(False)
                    ghost.seek(atv_est + off, now)
                    log.info("ATV resumed -> resync to %.1fs", atv_est + off)
                elif atv_paused:
                    if not ghost.paused:
                        ghost.set_pause(True)
                        log.info("ATV paused -> ghost paused")
                else:
                    target = atv_est + off
                    drift = ghost.pos - target
                    seek_ok = now - ghost.last_seek >= float(cfg["seek_cooldown_s"])
                    if abs(drift) >= float(cfg["seek_threshold_s"]) and seek_ok:
                        ghost.seek(target, now)
                        log.info("drift %.2fs -> seek to %.1fs", drift, target)
                    elif abs(drift) >= float(cfg["nudge_threshold_s"]):
                        d = float(cfg["max_speed_delta"])
                        ghost.set_speed(1.0 - (d if drift > 0 else -d))
                    else:
                        ghost.set_speed(1.0)
            else:
                if idle_since is None:
                    idle_since = now
                    if w["seen"]:
                        log.info("watched device idle")
                atv_item = None
                atv_paused = True
                if (ghost is not None and idle_since is not None
                        and now - idle_since >= float(cfg["idle_stop_delay_s"])):
                    log.info("ATV stopped -> stopping ghost")
                    ghost.kill()
                    ghost = None
                    poke_huestacean(cfg.get("huestacean_url", ""), "stop")

            time.sleep(poll)
    finally:
        if ghost is not None:
            try:
                ghost.kill()
            except Exception:
                pass
        poke_huestacean(cfg.get("huestacean_url", ""), "stop")


def test_mpv(cfg, video):
    print("test-mpv: launching", video)
    args = ghost_video_args(cfg, "hue-ghost-test")
    args += ["--start=5", os.path.abspath(video)]
    errf = os.path.join(HERE, "mpv_test.err")
    with open(errf, "wb") as ef:
        proc = subprocess.Popen(args, cwd=HERE, stdout=subprocess.DEVNULL, stderr=ef)
    g = Ghost(proc, "test", 5.0, cfg["ipc_pipe"])
    try:
        for step in (("set_property", "speed", 1.02),
                     ("seek", 15, "absolute", "exact"),
                     ("set_property", "pause", True),
                     ("set_property", "pause", False)):
            time.sleep(1.5)
            if not g.alive():
                with open(errf, "rb") as f:
                    print("mpv stderr:", f.read().decode("utf-8", "replace")[:800])
                raise RuntimeError("mpv died during test (rc=%s)" % proc.poll())
            g.send(*step)
        g.advance(g.t + 2.0, frozen=False)
        print("PASS: IPC pipe open, 4 commands sent, estimator pos=%.2f" % g.pos)
        return 0
    except Exception as e:
        print("FAIL:", e)
        return 1
    finally:
        g.kill()


def main(argv):
    cfg = load_config()
    setup_logging(cfg)
    if len(argv) > 1 and argv[1] == "--test-mpv":
        if len(argv) < 3:
            print("usage: python hue_ghost.py --test-mpv <video-file>")
            return 2
        return test_mpv(cfg, argv[2])
    if not str(cfg.get("api_key", "")).strip():
        log.error("api_key is empty. Jellyfin Dashboard > API Keys > '+' -> "
                  "name it 'hue-ghost', paste into %s, then rerun.", CONFIG_PATH)
        return 2
    try:
        run(cfg)
    except KeyboardInterrupt:
        log.info("stopped by user")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
