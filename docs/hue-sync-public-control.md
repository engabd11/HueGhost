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
- `inc_bri` works any time; `step` is signed.
- Unknown commands are logged by the app (`Unknown command: %s`) and ignored;
  a message without `command` logs `Unknown message`. Neither closes the socket.
- Every *accepted* command is answered with an `app_state_update`; hue-ghost
  waits `RESEND_AFTER_S` (2.5 s) for it before re-sending.

## Useful read-only files

- `%APPDATA%\HueSync\config.json`: `PublicControlEnabled`, `PublicControlPort`,
  `AutomaticDisplay` (true = the app picks the display itself; set the ghost
  display manually), `SyncDelay` (ms), hotkeys.
- `%APPDATA%\HueSync\bridge.json`: `SelectedGroup` (entertainment area id) and
  `Groups[].Name`. Note: it embeds a PEM certificate with raw newlines, so parse
  with `json.loads(..., strict=False)`.

hue-ghost only ever writes one thing: `SelectedGroup` in `bridge.json`, and only
while the app is stopped (entertainment-area switching for player bindings):
stop own sync -> `taskkill HueSync.exe` -> substitute the id in the text (the
file is otherwise left byte-identical) -> relaunch `HueSync.exe -silent`. The
app reads the selection at start-up and rewrites the file itself; verified on
1.13.1. `-silent` starts it without showing the window.
