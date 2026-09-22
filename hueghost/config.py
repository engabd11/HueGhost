"""Configuration: nested schema, defaults, path resolution, legacy migration.

Resolution order for the config file:
  1. explicit path (``--config``)
  2. ``config.json`` in the current working directory
  3. ``config.json`` at the source checkout root, when running from source
  4. ``%APPDATA%\\hue-ghost\\config.json`` (``~/.config/hue-ghost/config.json``
     elsewhere) - the location ``hue-ghost setup`` writes to

A v1 flat config (``server_url``, ``watch_device`` ...) is migrated on load and
keeps working unchanged.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from typing import Any

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "log_level": "INFO",
    "jellyfin": {
        "url": "http://127.0.0.1:8096",
        "api_key": "",
        "follow": {
            "device_id": "",             # exact Jellyfin DeviceId (preferred)
            "device_name_contains": "",  # case-insensitive substring of DeviceName + Client
            "user": "",                  # optional UserName substring
        },
        # More players, in priority order, each optionally bound to a Hue
        # entertainment area: {device_id, device_name_contains, user, area_id, area_name}.
        # `follow` above is the first player (its area: jellyfin.follow_area_id).
        "players": [],
        "follow_area_id": "",
        "follow_area_name": "",
        "poll_interval_s": 0.5,
    },
    # Everything that can drive the lights, in priority order - Jellyfin clients
    # and apps on this PC. Canonical; empty means "derive from jellyfin.follow
    # and jellyfin.players above", which is what a config written before 2.4 has.
    "sources": [],
    "pc": {
        "poll_interval_s": 1.0,      # detection rate; the 0.25 s tick never touches it
        "audio_peak": 0.002,         # a session louder than this counts as playing
        "audio_hold_s": 3.0,         # keep "playing" through dialogue gaps and track changes
        "start_confirm_s": 1.0,      # ignore anything shorter (alt-tab, a notification chime)
    },
    "sync": {
        "offset_s": 1.5,             # ghost runs this far AHEAD of the followed client
        "seek_threshold_s": 1.0,     # beyond this drift -> hard seek
        "deadband_s": 0.05,          # within this -> leave speed at 1.0
        "converge_s": 5.0,           # proportional controller time constant
        "max_speed_delta": 0.04,     # +/- speed clamp for nudging
        "seek_cooldown_s": 3.0,
        "jitter_tolerance_s": 1.5,   # report vs model difference treated as jitter, not a seek
        "lights_off_delay_s": 1.5,   # lights stop this long after the client stops (ghost stays on standby)
        "idle_stop_delay_s": 10.0,   # ... and the ghost mpv closes after this long
        "pause_stop_min": 0.0,       # stop syncing after the client is paused this
                                     # many minutes (0 = never); resumes on play
        "stall_estimates": {},       # learned client buffering after seek/start/resume (auto-saved)
    },
    "ghost": {
        "mpv_path": "mpv",
        "fullscreen": True,
        "screen_name": "",           # e.g. \\\\.\\DISPLAY3 -> mpv --fs-screen-name
        "screen_index": None,        # integer -> mpv --fs-screen=N
        "geometry": "28%x28%-40-40",
        "ipc": "",                   # empty -> platform default pipe/socket
        "hwdec": "auto",
        "keep_awake": "playing",     # off | playing | always: hold the displays awake (Windows idle timeout
                                     # switches the virtual ghost display off -> Hue Sync captures nothing)
        "audio_device": "",          # render endpoint a music ghost plays into, and that Hue Sync
                                     # listens to in music mode: pick one you cannot hear
        "music_volume": 100,
        "relaunch_cooldown_s": 5.0,
        "relaunch_max_per_5min": 6,
        "extra_args": [],
    },
    "engine": {
        "type": "huesync",           # huesync | httphook | none
        "huesync": {
            "host": "127.0.0.1",
            "port": 24851,
            "mode": "video",         # video | music | games (what Hue Sync reacts to)
            "intensity": "high",     # subtle | moderate | high | extreme
            "use_audio": None,       # "use audio for light effects" in video/games mode:
                                     # None = leave the app's own setting alone, True/False = enforce
            "manage_area": True,     # False -> never take the app's entertainment area over
            "manage_monitor": True,  # False -> never change which display it captures
            "manage_audio_device": True,   # False -> never change its music-mode input
            "required": False,       # True -> ghost only runs when Hue Sync is reachable
            "launch_exe": "",        # optional path to HueSync.exe to start when absent
        },
        "httphook": {"url": ""},     # GET <url>/start and <url>/stop
    },
    "control": {
        "bind": "127.0.0.1",         # 0.0.0.0 to expose to Home Assistant
        "port": 8787,
        "token": "",                 # bearer token when exposed
    },
}

INTENSITIES = ("subtle", "moderate", "high", "extreme")
MODES = ("video", "music", "games")      # Hue Sync also has "scenes"; it syncs nothing
ENGINE_TYPES = ("huesync", "httphook", "none")
KEEP_AWAKE_MODES = ("off", "playing", "always")

DETECT_MODES = ("audio", "fullscreen", "either")
SOURCE_KINDS = ("jellyfin", "pc")
MEDIA_KINDS = ("video", "music")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "binding"


def _binding_selector(b: dict) -> str:
    """What identifies the thing: an exe for a PC app, a device for Jellyfin."""
    if b.get("source") == "pc":
        return str(b.get("exe", "") or "")
    return str(b.get("device_id", "") or b.get("device_name_contains", "") or "")


def _binding(b: dict) -> dict:
    """One binding, every key present. New fields must be defaulted here and
    nowhere else - this is the only shape the rest of the app ever sees."""
    src = b.get("source") if b.get("source") in SOURCE_KINDS else "jellyfin"
    mode = str(b.get("mode", "") or "").lower()
    if src == "pc" and mode not in MODES:
        mode = "video"
    detect = str(b.get("detect", "") or "").lower()
    if detect not in DETECT_MODES:
        # sound is what tells a playing video from an open window; a game is
        # usually silent-ish in menus but always foreground and fullscreen
        detect = "fullscreen" if mode == "games" else "audio"
    kinds = [k for k in (b.get("kinds") or MEDIA_KINDS) if k in MEDIA_KINDS] or list(MEDIA_KINDS)
    name = str(b.get("name", "") or "")
    out = {
        "id": str(b.get("id", "") or "") or _slug(name or _binding_selector(b)),
        "enabled": bool(b.get("enabled", True)),
        "source": src,
        "name": name,
        "device_id": str(b.get("device_id", "") or ""),
        "device_name_contains": str(b.get("device_name_contains", "") or ""),
        "user": str(b.get("user", "") or ""),
        "kinds": kinds,
        "exe": str(b.get("exe", "") or "").lower(),
        "detect": detect,
        "mode": mode,
        "area_id": str(b.get("area_id", "") or ""),
        "area_name": str(b.get("area_name", "") or ""),
        "monitor": str(b.get("monitor", "") or ""),
        "audio_device": str(b.get("audio_device", "") or ""),
    }
    if not out["name"]:
        out["name"] = out["exe"] or out["device_name_contains"] or out["device_id"]
    return out

_LEGACY_MAP = {
    "server_url": "jellyfin.url",
    "api_key": "jellyfin.api_key",
    "watch_device": "jellyfin.follow.device_name_contains",
    "watch_user": "jellyfin.follow.user",
    # poll_interval_s deliberately not migrated: v1 polled at 1 s, the new
    # position model is tuned for the 0.5 s default
    "seek_threshold_s": "sync.seek_threshold_s",
    "max_speed_delta": "sync.max_speed_delta",
    "sync_offset_s": "sync.offset_s",
    "seek_cooldown_s": "sync.seek_cooldown_s",
    "idle_stop_delay_s": "sync.idle_stop_delay_s",
    "relaunch_cooldown_s": "ghost.relaunch_cooldown_s",
    "relaunch_max_per_5min": "ghost.relaunch_max_per_5min",
    "mpv_path": "ghost.mpv_path",
    "ghost_fullscreen": "ghost.fullscreen",
    "ghost_geometry": "ghost.geometry",
    "ipc_pipe": "ghost.ipc",
    "huestacean_url": "engine.httphook.url",
    "control_port": "control.port",
    "enabled": "enabled",
    "log_level": "log_level",
}

APP_DIR_NAME = "hue-ghost"


def app_data_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_DIR_NAME)


def source_root() -> str | None:
    """Repo root when running from a source checkout (has pyproject.toml)."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    if os.path.exists(os.path.join(root, "pyproject.toml")):
        return root
    return None


def resolve_config_path(explicit: str | None = None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    cwd = os.path.join(os.getcwd(), "config.json")
    if os.path.exists(cwd):
        return cwd
    root = source_root()
    if root and os.path.exists(os.path.join(root, "config.json")):
        return os.path.join(root, "config.json")
    return os.path.join(app_data_dir(), "config.json")


def log_path() -> str:
    root = source_root()
    base = root if root else app_data_dir()
    return os.path.join(base, "hue-ghost.log")


def default_ipc_path() -> str:
    if sys.platform == "win32":
        return r"\\.\pipe\hue-ghost"
    return os.path.join(os.environ.get("TMPDIR", "/tmp"), "hue-ghost.sock")


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _set_path(d: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def _get_path(d: dict, dotted: str, default: Any = None) -> Any:
    for p in dotted.split("."):
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return d


def is_legacy(raw: dict) -> bool:
    return "server_url" in raw or "watch_device" in raw


def migrate_legacy(raw: dict) -> dict:
    """Map a v1 flat config onto the nested schema (values only; defaults fill the rest)."""
    out: dict[str, Any] = {}
    for old, new in _LEGACY_MAP.items():
        if old in raw and raw[old] is not None:
            _set_path(out, new, raw[old])
    if raw.get("huestacean_url"):
        _set_path(out, "engine.type", "httphook")
    return out


class Config:
    """Dict-backed config with dotted-path access and live reload."""

    def __init__(self, data: dict | None = None, path: str | None = None):
        self.path = path
        self.data = _deep_merge(DEFAULTS, data or {})
        self.migrated = False

    # -- access -----------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        v = _get_path(self.data, dotted, default)
        if dotted == "ghost.ipc" and not v:
            return default_ipc_path()
        return v

    def set(self, dotted: str, value: Any) -> None:
        _set_path(self.data, dotted, value)

    def section(self, name: str) -> dict:
        return self.data.get(name, {})

    # -- persistence ------------------------------------------------------
    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = resolve_config_path(path)
        if not os.path.exists(path):
            return cls({}, path)
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if is_legacy(raw):
            cfg = cls(migrate_legacy(raw), path)
            cfg.migrated = True
        else:
            cfg = cls(raw, path)
        return cfg

    def reload(self) -> None:
        fresh = Config.load(self.path)
        self.data = fresh.data
        self.migrated = fresh.migrated

    def save(self, path: str | None = None) -> str:
        path = path or self.path or resolve_config_path(None)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        self.path = path
        return path

    def exists(self) -> bool:
        return bool(self.path) and os.path.exists(self.path)

    # -- players ------------------------------------------------------------
    def players(self) -> list[dict]:
        """All followed players in priority order (the legacy single `follow`
        first), normalised to {device_id, device_name_contains, user, area_id, area_name}."""
        out = []
        f = self.get("jellyfin.follow", {}) or {}
        if f.get("device_id") or f.get("device_name_contains"):
            out.append({"device_id": f.get("device_id", "") or "", "device_name_contains": f.get("device_name_contains", "") or "",
                        "user": f.get("user", "") or "", "area_id": self.get("jellyfin.follow_area_id", "") or "",
                        "area_name": self.get("jellyfin.follow_area_name", "") or ""})
        for p in self.get("jellyfin.players", []) or []:
            if not isinstance(p, dict) or not (p.get("device_id") or p.get("device_name_contains")):
                continue
            out.append({"device_id": p.get("device_id", "") or "", "device_name_contains": p.get("device_name_contains", "") or "",
                        "user": p.get("user", "") or "", "area_id": p.get("area_id", "") or "",
                        "area_name": p.get("area_name", "") or ""})
        return out

    def set_players(self, players: list[dict]) -> None:
        """Store a full players list: first entry -> `follow`, the rest -> `players`."""
        players = [p for p in players if p.get("device_id") or p.get("device_name_contains")]
        first = players[0] if players else {"device_id": "", "device_name_contains": "", "user": "", "area_id": "", "area_name": ""}
        self.set("jellyfin.follow", {"device_id": first.get("device_id", ""), "device_name_contains": first.get("device_name_contains", ""),
                                     "user": first.get("user", "")})
        self.set("jellyfin.follow_area_id", first.get("area_id", "") or "")
        self.set("jellyfin.follow_area_name", first.get("area_name", "") or "")
        self.set("jellyfin.players", [{"device_id": p.get("device_id", ""), "device_name_contains": p.get("device_name_contains", ""),
                                       "user": p.get("user", ""), "area_id": p.get("area_id", "") or "",
                                       "area_name": p.get("area_name", "") or ""} for p in players[1:]])

    # -- bindings ---------------------------------------------------------
    def bindings(self) -> list[dict]:
        """Everything that can drive the lights, in priority order: Jellyfin
        clients and apps on this PC, normalised to one shape.

        ``sources`` is canonical. An older config has none, so it is derived
        from ``jellyfin.follow``/``jellyfin.players`` - which stay mirrored by
        ``set_bindings`` so a downgrade still finds its players."""
        raw = self.get("sources", None)
        if not raw:
            return [_binding(dict(p, source="jellyfin")) for p in self.players()]
        return [_binding(b) for b in raw
                if isinstance(b, dict) and _binding_selector(b)]

    def set_bindings(self, bindings: list[dict]) -> None:
        """Store the full list. The Jellyfin half is mirrored back into
        ``jellyfin.follow``/``players`` so every existing reader keeps working."""
        out, seen = [], set()
        for b in bindings:
            b = _binding(b)
            if not _binding_selector(b):
                continue
            base, n = b["id"], 2
            while b["id"] in seen:          # ids address a binding over the API
                b["id"], n = "%s-%d" % (base, n), n + 1
            seen.add(b["id"])
            out.append(b)
        self.set("sources", out)
        self.set_players([p for p in out if p.get("source") == "jellyfin"])

    def enabled_bindings(self) -> list[dict]:
        return [b for b in self.bindings() if b.get("enabled", True)]

    # -- validation -------------------------------------------------------
    def binding_problems(self, b: dict) -> list[str]:
        """What stops one binding working. Non-fatal: the daemon skips it and
        says so, rather than refusing to run at all."""
        out = []
        if b.get("source") == "pc":
            if not str(b.get("exe", "")).strip():
                out.append("no application chosen")
            if b.get("mode") not in MODES:
                out.append("mode must be one of " + " | ".join(MODES))
            if b.get("detect") not in DETECT_MODES:
                out.append("detect must be one of " + " | ".join(DETECT_MODES))
        else:
            if not (b.get("device_id") or b.get("device_name_contains")):
                out.append("no Jellyfin device chosen")
            if not str(self.get("jellyfin.url", "")).strip():
                out.append("jellyfin.url is empty")
            if not str(self.get("jellyfin.api_key", "")).strip():
                out.append("jellyfin.api_key is empty (Jellyfin Dashboard > API Keys > +)")
            if b.get("kinds") == ["music"] or "music" in (b.get("kinds") or []):
                if not (b.get("audio_device") or self.get("ghost.audio_device")):
                    out.append("music needs an output for the ghost to play into "
                               "(Sources > Audio input, or Display > Ghost audio output)")
        return out

    def problems(self) -> list[str]:
        """What stops the daemon running at all. A Jellyfin server that is not
        set up is only fatal when something actually follows it - a PC-only
        install is a perfectly good setup."""
        out = []
        binds = self.enabled_bindings()
        jelly = [b for b in binds if b.get("source") == "jellyfin"]
        if jelly:
            if not str(self.get("jellyfin.api_key", "")).strip():
                out.append("jellyfin.api_key is empty (Jellyfin Dashboard > API Keys > +)")
            if not str(self.get("jellyfin.url", "")).strip():
                out.append("jellyfin.url is empty")
        if not binds:
            out.append("nothing to follow (add a Jellyfin player or an app on this PC)")
        if self.get("engine.type") not in ENGINE_TYPES:
            out.append("engine.type must be one of " + " | ".join(ENGINE_TYPES))
        if self.get("engine.huesync.intensity") not in INTENSITIES:
            out.append("engine.huesync.intensity must be one of " + " | ".join(INTENSITIES))
        if self.get("engine.huesync.mode") not in MODES:
            out.append("engine.huesync.mode must be one of " + " | ".join(MODES))
        return out
