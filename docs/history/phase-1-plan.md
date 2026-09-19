# hue-ghost — Phase 1: Hue Sync as the engine, hands-free

Status: planned (approved pending — Abdullah, Sep 2026)
Builds on: working v1 daemon (session watcher, mpv ghost lockstep, control API)

## The idea

Phase 1 turns hue-ghost from a manually-toggled demo into a hands-free system.
The daemon already watches the Apple TV's Jellyfin session and keeps a muted
ghost playback in lockstep on this PC. Phase 1 adds the two missing pieces:

1. **Drive the official Hue Sync PC app over its Public Control WebSocket**
   (reverse-engineered and live-verified: WS on 0.0.0.0:24851, commands
   `start_sync` / `stop_sync` / `inc_bri` / `set_intensity` / `set_app_mode`,
   events `app_state_update` with state/mode/intensity/bri). Sync starts when
   the ATV starts, stops when it stops — no manual toggling, no Huestacean,
   no AHK.
2. **Move the ghost off the physical screen** onto a virtual display driver
   monitor, so the ultrawide stays fully usable while Hue Sync samples the
   ghost there (Hue Sync's Display tab can pick any display; set once).

Result: press play on the ATV → lights come on in sync automatically; stop →
lights stop; PC stays usable the whole time.

## Architecture after Phase 1

```
Jellyfin server (192.168.0.156:8096)
   │  Sessions API (1s poll)
   ▼
hue-ghost daemon ──► mpv ghost (fullscreen on VIRTUAL display, muted)
   │   lockstep: speed nudges / seeks, offset +1.5s
   ▼
Hue Sync PC app  ◄── WS :24851  (start_sync / stop_sync / set_app_mode video)
   │  samples the virtual display (one-time manual selection)
   ▼
Hue Bridge ──► living-room entertainment area
```

## Scope

**In scope**
- `HueSyncController`: WS client for the Public Control API — auto-reconnect,
  state tracking from `app_state_update`, command queue, graceful degradation.
- Daemon wiring: ghost actually rendering → `start_sync`; ghost stop / idle /
  user close / daemon exit → `stop_sync` (with small settle delay).
- Mode & intensity automation: `set_app_mode video` on start; configured
  intensity; optional brightness cap via `inc_bri` steps only if needed.
- Virtual display spike: install + evaluate a virtual display driver (e.g.
  Virtual Display Driver / virtual-display-rs), point mpv at it
  (`--fs-screen=N`), point Hue Sync's Display tab at it (manual, once).
- Failure handling: Hue Sync not running / toggle off / port changed →
  backoff + reconnect, ghost keeps playing, clear log lines.
- Config additions: `hsync_enabled`, `hsync_port`, `hsync_mode`,
  `hsync_intensity`, `ghost_screen` (fs-screen index), all `/reload`-live.

**Out of scope (Phase 2+)**
- DTLS engine / precomputed color timelines (Route B), HA entities, tray
  packaging/installer, multi-room, running on the server box.

## Milestones & acceptance criteria

**M1 — HueSyncController module** (~2h)
- stdlib WS client (same plumbing as the probes: handshake, masked frames,
  ping/pong), state machine: disconnected → connected(state known) → syncing.
- Offline test: mock WS server replaying `app_state_update`; assert command
  emission for start/stop and unknown-command tolerance.
- Accept: mock test passes; against the real app: `start_sync` flips state to
  `syncing`, `stop_sync` back to `bridge_connected`.

**M2 — Daemon integration** (~1h)
- Launch ghost → confirm mpv alive → `start_sync`; idle/stop/disable/close →
  `stop_sync`; daemon `finally` → `stop_sync`.
- Keep `huestacean_url` path as optional fallback; new path is default.
- Accept: full cycle test — press play on ATV → lights sync with zero manual
  steps; stop the episode → lights stop ≤10s; restart daemon mid-movie →
  sync resumes.

**M3 — Robustness** (~1h)
- Hue Sync killed mid-movie → daemon logs + keeps ghost + retries (backoff),
  sync resumes automatically when the app returns (or stays silent per
  config flag `hsync_required: false`).
- Accept: kill HueSync.exe during playback → no crash, recovery verified.

**M4 — Virtual display spike** (~1 evening, has a fallback)
- Install virtual display driver; confirm it appears to Windows + Hue Sync
  Display tab; mpv fullscreen on it; Hue Sync captures; colors reach lights.
- Accept: watch a movie while normally using the PC on the ultrawide; lights
  track the movie; no color wash-out visible.
- **Fallback:** if the driver is flaky or capture quality is poor → keep
  current fullscreen-on-ultrawide mode; M1–M3 still ship value.

**M5 — Polish** (~1h)
- README rewrite of the Hue-side setup (toggle ON, port, one-time display
  pick); update hue-ghost skill; log improvements.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Virtual display driver unstable / washed-out capture | M4 fallback: stay on physical fullscreen; keep M1–M3 gains |
| Hue Sync update changes protocol | Protocol documented in skill; controller tolerates unknown events/commands |
| WS unauthenticated, bound to 0.0.0.0 | We connect to 127.0.0.1 only; never port-forward; note in README |
| Hue Sync closed by user | Detect via state/WS-down; per config either ignore or keep retrying |
| Entertainment area deselected | Manual set-once; documented in README |

## Effort

M1–M3: one evening. M4: one evening (spike, with fallback). Total ≈ 2 evenings
to a fully hands-free Phase 1.
