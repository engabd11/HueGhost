"""Local/LAN HTTP control API (what Home Assistant, the tray and the CLI talk to).

  GET  /status            full state snapshot (JSON)
  GET  /health            {"ok": true, "version": ...}   (no auth)
  POST /on | /off | /toggle | /reload      (GET accepted too, for curl / .bat files)
  POST /set  {"enabled": bool, "mode": "video|music|games", "intensity": "high",
              "use_audio": true|false|null, "manage_area": bool, "offset_s": 1.5,
              "offset_delta": 0.25, "brightness_step": 10}
       (GET /set?offset_delta=0.25 ... accepted too)

Bind to 127.0.0.1 (default) or 0.0.0.0 + ``control.token`` (sent as
``Authorization: Bearer <token>`` or ``?token=``) when exposing to the LAN.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import __version__

log = logging.getLogger("hue-ghost.control")

ACTION_PATHS = ("/on", "/off", "/toggle", "/reload", "/set")


def _coerce(v: str):
    lv = v.lower()
    if lv in ("true", "false"):
        return lv == "true"
    try:
        return float(v) if ("." in v or "e" in lv) else int(v)
    except ValueError:
        return v


class ControlServer:
    def __init__(self, bind: str, port: int, token: str,
                 status_fn: Callable[[], dict],
                 action_fn: Callable[[str, dict], dict],
                 api_fn: Callable[[str, str, dict, dict], object] | None = None):
        self.bind, self.port, self.token = bind, int(port), token or ""
        self.status_fn, self.action_fn, self.api_fn = status_fn, action_fn, api_fn
        self._srv: ThreadingHTTPServer | None = None

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "hue-ghost/" + __version__

            def _send(self, code: int, obj) -> None:
                data = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except Exception:
                    pass

            def _authed(self, query: dict) -> bool:
                if not outer.token:
                    return True
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Bearer ") and auth[7:].strip() == outer.token:
                    return True
                return query.get("token", [""])[0] == outer.token

            def _body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    return {}
                raw = self.rfile.read(n)
                try:
                    obj = json.loads(raw.decode("utf-8", "replace"))
                    return obj if isinstance(obj, dict) else {}
                except ValueError:
                    return {}

            def _serve(self, method: str) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                if path == "/health":
                    return self._send(200, {"ok": True, "version": __version__})
                if method == "GET" and path == "/":
                    return self._send(200, {"app": "hue-ghost", "version": __version__,
                                            "hint": "settings live in the Hue Ghost app; API under /api/"})
                if not self._authed({k: [v] for k, v in query.items()}):
                    return self._send(401, {"error": "unauthorized"})
                try:
                    if path.startswith("/api/"):
                        if outer.api_fn is None:
                            return self._send(404, {"error": "no api"})
                        payload = self._body() if method == "POST" else {}
                        try:
                            return self._send(200, outer.api_fn(method, path, payload, query))
                        except Exception as e:  # ApiError carries its code
                            code = getattr(e, "code", None)
                            if isinstance(code, int):
                                return self._send(code, {"error": str(e)})
                            raise
                    if path == "/status":
                        return self._send(200, outer.status_fn())
                    if path in ACTION_PATHS:
                        payload = self._body() if method == "POST" else {}
                        for k, v in query.items():
                            if k != "token":
                                payload.setdefault(k, _coerce(v))
                        return self._send(200, outer.action_fn(path[1:], payload))
                    return self._send(404, {"error": "unknown path",
                                            "paths": ["/status", "/health", *ACTION_PATHS]})
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:
                    log.exception("control request failed")
                    return self._send(500, {"error": str(e)})

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

            def log_message(self, fmt, *args):
                pass

        self._srv = ThreadingHTTPServer((self.bind, self.port), Handler)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, name="control-http", daemon=True).start()
        log.info("control API on http://%s:%d (%s)", self.bind, self.port,
                 "token required" if self.token else "no token")

    def stop(self) -> None:
        if self._srv:
            try:
                self._srv.shutdown()
                self._srv.server_close()
            except Exception:
                pass
            self._srv = None
