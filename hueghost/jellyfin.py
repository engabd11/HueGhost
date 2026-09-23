"""Minimal Jellyfin REST client (stdlib) + helpers for session bookkeeping."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

TICKS_PER_S = 10_000_000
_ZERO_DATE = "0001-01-01"
_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


def parse_jf_datetime(s: str | None) -> float | None:
    """Jellyfin ISO-8601 (7 fractional digits, 'Z') -> unix epoch seconds, or None.

    ``0001-01-01T00:00:00.0000000Z`` (Jellyfin's "never") -> None.
    """
    if not s or s.startswith(_ZERO_DATE):
        return None
    m = _ISO_RE.match(s.strip())
    if not m:
        return None
    base, frac, tz = m.groups()
    dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
    micro = int((frac or "0")[:6].ljust(6, "0"))
    dt = dt.replace(microsecond=micro)
    if tz in (None, "Z"):
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        sign = 1 if tz[0] == "+" else -1
        hh, mm = int(tz[1:3]), int(tz[-2:])
        from datetime import timedelta
        dt = dt.replace(tzinfo=timezone(sign * timedelta(hours=hh, minutes=mm)))
    return dt.timestamp()


class JellyfinError(Exception):
    pass


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 5.0):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout
        self._msid_cache: dict[str, list[dict]] = {}

    # -- transport --------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": 'MediaBrowser Token="%s", Client="hue-ghost", Device="hue-ghost", '
                             'DeviceId="hue-ghost", Version="2"' % self.key,
            "Accept": "application/json",
        }

    def _get(self, path: str) -> tuple[Any, float | None, float]:
        """GET -> (json, server_epoch_from_Date_header, local_epoch_at_response)."""
        req = urllib.request.Request(self.base + path, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
                date_hdr = r.headers.get("Date")
        except urllib.error.HTTPError as e:
            raise JellyfinError("HTTP %s for %s" % (e.code, path)) from e
        except Exception as e:  # URLError, timeout, ConnectionReset ...
            raise JellyfinError(str(e)) from e
        local = time.time()
        server = None
        if date_hdr:
            try:
                server = parsedate_to_datetime(date_hdr).timestamp()
            except Exception:
                server = None
        try:
            data = json.loads(body.decode("utf-8", "replace")) if body else None
        except ValueError as e:
            raise JellyfinError("bad JSON from %s" % path) from e
        return data, server, local

    # -- API --------------------------------------------------------------
    def public_info(self) -> dict:
        data, _, _ = self._get("/System/Info/Public")
        return data or {}

    def sessions(self) -> tuple[list[dict], float | None, float]:
        data, server, local = self._get("/Sessions")
        return (data or []), server, local

    def media_sources(self, item_id: str) -> list[dict]:
        if item_id not in self._msid_cache:
            data, _, _ = self._get("/Items?ids=%s&fields=MediaSources" % urllib.parse.quote(item_id))
            items = (data or {}).get("Items") or []
            self._msid_cache[item_id] = (items[0].get("MediaSources") or []) if items else []
        return self._msid_cache[item_id]

    def stream_url(self, item_id: str, media_source_id: str | None = None, kind: str = "video") -> str:
        """Direct-stream URL of the original file (no Jellyfin playback session).

        Music lives under /Audio; the ghost plays it with no video at all."""
        where = "Audio" if kind == "music" else "Videos"
        url = "%s/%s/%s/stream?static=true" % (self.base, where, item_id)
        msid = media_source_id
        if not msid:
            try:
                srcs = self.media_sources(item_id)
                msid = srcs[0].get("Id") if srcs else None
            except JellyfinError:
                msid = None
        if msid:
            url += "&MediaSourceId=" + urllib.parse.quote(str(msid))
        return url

    def auth_header_for_mpv(self) -> str:
        return 'Authorization: MediaBrowser Token="%s"' % self.key

    def primary_image(self, item_id: str, max_width: int = 240) -> bytes | None:
        """Artwork for the UI: the item's Primary image (episode thumb / movie
        poster) as encoded bytes, or None when there is none."""
        path = "/Items/%s/Images/Primary?maxWidth=%d&quality=85" % (urllib.parse.quote(item_id), int(max_width))
        req = urllib.request.Request(self.base + path, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read() or None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise JellyfinError("HTTP %s for %s" % (e.code, path)) from e
        except Exception as e:
            raise JellyfinError(str(e)) from e


def session_label(s: dict) -> str:
    return "%s / %s%s" % (s.get("DeviceName") or "?", s.get("Client") or "?",
                          (" (%s)" % s["UserName"]) if s.get("UserName") else "")


def label_sessions(rows: list[dict]) -> None:
    """Give each discovered session a name that tells it apart from the others.

    Phones are the problem. Jellyfin reports most of them with a generic
    DeviceName - two different handsets both arrive as plain "Android" - so the
    device name alone is never enough to pick the right one from a list. The app
    and the signed-in user are what actually distinguish them, and when even
    those repeat there is still the device id, which Jellyfin issues per app
    install (which is also why one phone can hold a separate entry, and so a
    separate configuration, for its music app and its video app).

    Sets ``label`` on every row, in place."""
    keys = [((r.get("device_name") or "?"), (r.get("client") or "?"), (r.get("user") or "")) for r in rows]
    for r, k in zip(rows, keys):
        label = "%s - %s" % (k[0], k[1])
        if k[2]:
            label += " (%s)" % k[2]
        if keys.count(k) > 1:
            # the *tail*: Jellyfin ids for one app often share a long prefix
            # (the Android app grew its id scheme and left the old one behind),
            # so the front of the string is exactly the part that repeats
            label += "  [...%s]" % (r.get("device_id") or "?")[-6:]
        r["label"] = label


def item_display_name(item: dict) -> str:
    """'Series - S01E02 - Title' for episodes, plain Name otherwise."""
    name = item.get("Name") or "?"
    if item.get("Type") == "Episode" and item.get("SeriesName"):
        season = item.get("ParentIndexNumber")
        ep = item.get("IndexNumber")
        code = ""
        if season is not None and ep is not None:
            code = " - S%02dE%02d" % (int(season), int(ep))
        return "%s%s - %s" % (item["SeriesName"], code, name)
    return name
