"""JSON API behind the settings UI (served by the control server under /api/).

Everything the UI can do goes through here, so the same endpoints work for
curl, Home Assistant or a phone on the LAN (with the control token).
"""
from __future__ import annotations

import logging
import os
import secrets
import subprocess
import sys
from typing import TYPE_CHECKING, Any

from . import __version__
from .config import INTENSITIES, MODES, app_data_dir, log_path
from .jellyfin import JellyfinClient, JellyfinError, item_display_name, label_sessions
from .pcwatch import list_processes
from .winutil import (autostart_installed, default_audio_output_id, find_mpv, hue_sync_info,
                      install_autostart, list_audio_outputs, list_displays, tray_command,
                      uninstall_autostart)

if TYPE_CHECKING:
    from .daemon import Daemon

log = logging.getLogger("hue-ghost.api")


class ApiError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


class WebApi:
    def __init__(self, daemon: "Daemon"):
        self.d = daemon

    # -- dispatch ----------------------------------------------------------------------
    def handle(self, method: str, path: str, payload: dict, query: dict) -> Any:
        route = (method, path)
        fn = self._routes().get(route)
        if fn is None:
            raise ApiError("unknown endpoint %s %s" % (method, path), 404)
        return fn(payload, query)

    def _routes(self):
        return {
            ("GET", "/api/status"): lambda p, q: self.d.status(),
            ("GET", "/api/config"): lambda p, q: self.d.cfg.data,
            ("POST", "/api/config"): lambda p, q: self.d.apply_config(p),
            ("GET", "/api/sessions"): self.sessions,
            ("GET", "/api/displays"): lambda p, q: {"displays": [d.__dict__ for d in list_displays()]},
            ("GET", "/api/areas"): lambda p, q: {"areas": self.d.engine.areas(), "players": self.d.cfg.players()},
            ("GET", "/api/bindings"): lambda p, q: {"bindings": self.d._binding_status(),
                                                    "areas": self.d.engine.areas()},
            ("POST", "/api/bindings"): self.set_bindings,
            ("GET", "/api/processes"): lambda p, q: {"processes": list_processes()},
            ("GET", "/api/audio"): lambda p, q: {"outputs": [a.__dict__ | {"mpv_device": a.mpv_device}
                                                             for a in list_audio_outputs()],
                                                 "default": default_audio_output_id()},
            ("GET", "/api/modes"): lambda p, q: {"modes": list(MODES), "intensities": list(INTENSITIES)},
            ("GET", "/api/huesync"): self.huesync,
            ("GET", "/api/mpv"): self.mpv,
            ("POST", "/api/ghost/test"): lambda p, q: self.d.test_ghost(p.get("screen_name"), int(p.get("seconds", 8))),
            ("GET", "/api/autostart"): lambda p, q: {"enabled": autostart_installed()},
            ("POST", "/api/autostart"): self.autostart,
            ("GET", "/api/log"): self.log_tail,
            ("POST", "/api/stalls/reset"): self.reset_stalls,
            ("GET", "/api/system"): self.system,
            ("POST", "/api/open"): self.open_thing,
            ("POST", "/api/token"): lambda p, q: {"token": secrets.token_urlsafe(24)},
            ("POST", "/api/restart"): self.restart,
            ("POST", "/api/set"): lambda p, q: self.d.action("set", p),
            ("POST", "/api/on"): lambda p, q: self.d.action("on", {}),
            ("POST", "/api/off"): lambda p, q: self.d.action("off", {}),
        }

    # -- handlers ----------------------------------------------------------------------
    def sessions(self, p: dict, q: dict) -> dict:
        url = (q.get("url") or self.d.cfg.get("jellyfin.url") or "").strip()
        key = (q.get("api_key") or self.d.cfg.get("jellyfin.api_key") or "").strip()
        if not url:
            raise ApiError("Jellyfin URL is empty")
        jf = JellyfinClient(url, key)
        try:
            info = jf.public_info()
        except JellyfinError as e:
            raise ApiError("cannot reach Jellyfin at %s: %s" % (url, e))
        if not key:
            return {"server": info, "sessions": [], "auth": False}
        try:
            sessions, _, _ = jf.sessions()
        except JellyfinError as e:
            raise ApiError("API key rejected by %s: %s" % (info.get("ServerName") or url, e))
        by_id: dict[str, dict] = {}
        for s in sessions:
            np = s.get("NowPlayingItem")
            row = {
                "device_id": s.get("DeviceId") or "",
                "device_name": s.get("DeviceName"),
                "client": s.get("Client"),
                "version": s.get("ApplicationVersion"),
                "user": s.get("UserName"),
                "now_playing": item_display_name(np) if np else None,
                "media_type": np.get("MediaType") if np else None,
                "last_activity": s.get("LastActivityDate"),
                "playable": bool(s.get("PlayableMediaTypes")),
            }
            # One app on one device is one entry, however many sessions Jellyfin
            # is holding open for it; the one actually playing is the useful one.
            prev = by_id.get(row["device_id"]) if row["device_id"] else None
            if prev is None or (row["now_playing"] and not prev["now_playing"]):
                by_id[row["device_id"] or ("#%d" % len(by_id))] = row
        out = list(by_id.values())
        label_sessions(out)
        out.sort(key=lambda x: (x["now_playing"] is None, not x["playable"], x["label"].lower()))
        return {"server": info, "sessions": out, "auth": True}

    def huesync(self, p: dict, q: dict) -> dict:
        info = hue_sync_info()
        host = q.get("host") or self.d.cfg.get("engine.huesync.host") or "127.0.0.1"
        try:
            port = int(q.get("port") or self.d.cfg.get("engine.huesync.port") or 24851)
        except ValueError:
            raise ApiError("bad port")
        from .wsclient import WebSocket, WebSocketError
        probe: dict = {"reachable": False, "state": None, "error": None}
        try:
            ws = WebSocket(host, port, "/", timeout=2.0).connect()
            try:
                ws.settimeout(2.0)
                import json as _json
                msg = _json.loads(ws.recv_text())
                probe.update(reachable=True, state=(msg.get("data") if isinstance(msg, dict) else None),
                             server=ws.server_header)
            finally:
                ws.close()
        except (OSError, WebSocketError, ValueError) as e:
            probe["error"] = str(e)
        info["probe"] = probe
        info["engine"] = self.d.engine.state().as_dict()
        return info

    def mpv(self, p: dict, q: dict) -> dict:
        path = find_mpv(q.get("path") or self.d.cfg.get("ghost.mpv_path") or "mpv")
        version = None
        if path:
            try:
                out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                version = (out.stdout or "").splitlines()[0] if out.stdout else None
            except Exception:
                version = None
        return {"path": path, "version": version, "found": bool(path)}

    def set_bindings(self, p: dict, q: dict) -> dict:
        """Replace the whole bindings list, in priority order. One binding can
        also be flipped on its own with POST /set {"binding": {...}}."""
        binds = p.get("bindings")
        if not isinstance(binds, list):
            raise ApiError('expected {"bindings": [...]}')
        import copy

        from .config import Config

        # a throwaway copy, so apply_config still sees a real change to diff
        tmp = Config(copy.deepcopy(self.d.cfg.data))
        tmp.set_bindings(binds)
        res = self.d.apply_config({"sources": tmp.get("sources"), "jellyfin": {
            "follow": tmp.get("jellyfin.follow"),
            "follow_area_id": tmp.get("jellyfin.follow_area_id"),
            "follow_area_name": tmp.get("jellyfin.follow_area_name"),
            "players": tmp.get("jellyfin.players")}})
        return dict(res, bindings=self.d._binding_status())

    def autostart(self, p: dict, q: dict) -> dict:
        want = bool(p.get("enabled"))
        if want:
            install_autostart(tray_command())
        else:
            uninstall_autostart()
        return {"enabled": autostart_installed()}

    def log_tail(self, p: dict, q: dict) -> dict:
        try:
            n = max(10, min(int(q.get("lines") or 200), 2000))
        except ValueError:
            n = 200
        path = log_path()
        lines: list[str] = []
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 256 * 1024))
                lines = f.read().decode("utf-8", "replace").splitlines()[-n:]
        except OSError:
            pass
        return {"path": path, "lines": lines}

    def reset_stalls(self, p: dict, q: dict) -> dict:
        from .watcher import STALL_PRIORS
        st = self.d.watcher.stalls
        st.est = dict(STALL_PRIORS)
        st.changed = True
        self.d.cfg.set("sync.stall_estimates", {})
        if self.d.cfg.path:
            self.d.cfg.save()
        return {"stall_estimates": dict(st.est)}

    def system(self, p: dict, q: dict) -> dict:
        return {
            "version": __version__,
            "config_path": self.d.cfg.path,
            "log_path": log_path(),
            "data_dir": app_data_dir(),
            "platform": sys.platform,
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": sys.executable,
            "autostart": autostart_installed(),
            "intensities": list(INTENSITIES),
            "modes": list(MODES),
            "ui_url": self.d.ui_url(),
            "restart_required": list(self.d.restart_required),
        }

    def open_thing(self, p: dict, q: dict) -> dict:
        what = p.get("what")
        targets = {"config": self.d.cfg.path, "log": log_path(),
                   "folder": os.path.dirname(self.d.cfg.path or app_data_dir())}
        path = targets.get(what)
        if not path:
            raise ApiError("what must be config | log | folder")
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            raise ApiError("could not open %s: %s" % (path, e), 500)
        return {"ok": True, "path": path}

    def restart(self, p: dict, q: dict) -> dict:
        self.d.request_restart()
        return {"ok": True, "restarting": True}
