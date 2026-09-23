# Hue Sync desktop app - "Public Control" WebSocket

Recovered from Hue Sync for Windows 1.13.1 (Oct 2025) and verified live.
Signify does not document this API; it exists for streamer tooling
(Stream Deck style plugins). Treat it as best effort across versions.

## Enabling

Hue Sync > Settings > **Allow public control**. Stored in
`%APPDATA%\HueSync\config.json` as `PublicControlEnabled` / `PublicControlPort`
(default **24851**). The server listens on all interfaces (`::`), unauthenticated,
so do not port-forward it; hue-ghost only ever connects to `127.0.0.1`.

## Transport

Plain WebSocket (RFC 6455), any request path. Handshake response:

```
HTTP/1.1 101 Switching Protocols
Server: Hue Sync Public Control
Access-Control-Allow-Origin: *
```

Text frames, one JSON object each. The app pings clients; answer pongs.

## Events (app -> client)

Sent on connect and after every accepted command / state change:

```json
{"event":"app_state_update", "data":{"state":"bridge_connected", "mode":"video", "intensity":"high", "bri":56}}
```

| field | values |
|---|---|
| `state` | `bridge_disconnected`, `bridge_connected`, `syncing` |
| `mode` | `video`, `games`, `music`, `scenes` |
| `intensity` | `subtle`, `moderate`, `high`, `extreme` |
| `bri` | 0-100 (the session brightness; changes when sync starts) |

## Commands (client -> app)

```json
{"command":"start_sync"}
{"command":"stop_sync"}
{"command":"set_app_mode",  "data":{"mode":"video"}}
{"command":"set_intensity", "data":{"intensity":"moderate"}}
{"command":"inc_bri",       "data":{"step":-5}}
```

Live-verified behaviour:

- `start_sync` from `bridge_connected` -> `syncing` in well under a second;
  `stop_sync` -> `bridge_connected` (the app then sends a few duplicate updates).
- **`set_app_mode` and `set_intensity` only act on an active sync session.**
  Sent while not syncing they are silently ignored (no reply). hue-ghost
  therefore reconciles `start_sync -> set_app_mode -> set_intensity`.
- Both are **live**: they take effect on the running session, in place, with no
  restart and no gap in the light stream. Nothing about a mode change requires
  touching the app's files.
- The app keeps an **intensity per mode** (`Core.AppMode.<Mode>.Default`), so a
  `set_app_mode` is answered with an update carrying *that mode's* intensity.
  That is the mode's doing, not a choice: hue-ghost re-asserts its own.
- An `app_state_update` that answers nothing we sent is the user at the app's
  own controls. hue-ghost adopts the mode/intensity from it rather than fighting
  it. Note the update answering `start_sync` still carries the app's *previous*
  mode, so it is not a choice either.
- `inc_bri` works any time; `step` is signed.
- Unknown commands are logged by the app (`Unknown command: %s`) and ignored;
  a message without `command` logs `Unknown message`. Neither closes the socket.
- Every *accepted* command is answered with an `app_state_update`; hue-ghost
  waits `RESEND_AFTER_S` (2.5 s) for it before re-sending.

**That is the whole vocabulary.** The command strings in `HueSync.exe` 1.13.1
are exactly `start_sync`, `stop_sync`, `inc_bri`, `set_intensity`,
`set_app_mode` - there is no command for the entertainment area, for
"use audio for light effects", or for anything else in the app's settings.
Those have to go through the app's own files, below.

## Settings the WebSocket cannot reach

Two things hue-ghost needs are only in the app's files, and the app reads them
at **start-up** - so applying one means: stop our sync -> `taskkill HueSync.exe`
-> patch the file -> relaunch `HueSync.exe -silent` (~3 s; the app rewrites the
file itself afterwards). hue-ghost does both in a single restart, only while it
wants sync, and only once per sync session - outside that the app is the user's.

| setting | file | key |
|---|---|---|
| entertainment area | `bridge.json` | `SelectedGroup` |
| use audio for light effects | `config.json` | `Core.AppMode.Video.WithAudio`, `Core.AppMode.Games.WithAudio` |

`Music` mode has no such key - it is audio by definition. The per-mode
intensity lives in the same place (`Core.AppMode.<Mode>.Default`, an index into
`Presets`), but `set_intensity` reaches that over the socket, so hue-ghost
leaves it alone.

### config.json is hash-guarded

Beside it, `%APPDATA%\HueSync\.cfg` holds a digest of `config.json`: the
**FNV-1a 64-bit** hash of the file's bytes (MSVC's `std::hash<std::string>`),
written as an unsigned decimal. Patch the config without updating it and the
app can take the file as changed underneath it. `.cfg` is a **hidden** file, so
Windows refuses `CREATE_ALWAYS` on it - truncate it in place (`r+`) instead of
reopening it with `"w"`. `bridge.json` has no such digest.

Verified on 1.13.1: patch `WithAudio` + `.cfg` while the app is stopped, relaunch,
and the value the app later writes back from memory is ours - it read the patch.

## Useful read-only files

- `%APPDATA%\HueSync\config.json`: `PublicControlEnabled`, `PublicControlPort`,
  `AutomaticDisplay` (true = the app picks the display itself; set the ghost
  display manually), `SyncDelay` (ms), hotkeys.
- `%APPDATA%\HueSync\bridge.json`: `SelectedGroup` (entertainment area id) and
  `Groups[].Name`. Note: it embeds a PEM certificate with raw newlines, so parse
  with `json.loads(..., strict=False)`.

Every write hue-ghost makes (see above) is a text substitution that leaves the
file byte-identical apart from the one value, and only ever happens while the
app is stopped. `-silent` starts it without showing the window.
