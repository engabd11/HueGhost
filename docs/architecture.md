# Architecture

```
Jellyfin server
   │  GET /Sessions every 0.5 s  (DeviceId match, LastPlaybackCheckIn, PositionTicks, IsPaused)
   ▼
watcher.py  ── PlaybackModel: anchor on NEW client reports only; clock-offset estimator;
   │            jitter vs seek discrimination; position_at(now)
   ▼
daemon.py   ── state machine  idle → ghosting → syncing
   │  every 250 ms:  target = model.position_at(now) + offset
   │                 actions = lockstep.decide(target, ghost_obs)      (pure)
   │                 ghost.apply(actions)                              (mpv IPC)
   ├─▶ ghost.py   mpv --fullscreen on the ghost display, muted; observe_property time-pos / pause /
   │              speed / paused-for-cache → real position readback
   ├─▶ engines/   Engine.start()/stop(): HueSyncEngine (WS :24851, declarative reconcile),
   │              HttpHookEngine (GET url/start|stop), NullEngine
   └─▶ control.py + webapi.py  HTTP API :8787 → Home Assistant / CLI
gui/app.py  ── PySide6 desktop app: runs the daemon in-process, tray icon, pages call the daemon directly
```

## Why the ghost follows and never leads

The TV is the source of truth; the PC copies it. Nothing hue-ghost does can
pause, seek or stall the TV. (This is also why Jellyfin SyncPlay is not used:
group members are peers, and a buffering peer pauses everyone.)

## Position model (watcher.py)

Jellyfin's `/Sessions` returns the *last reported* position of a client, and
clients report every ~5-10 s (plus immediately on play/pause/seek). Naively
re-anchoring on every poll (v1) turns that into 1-4 s corrections.

1. **Anchor only on a new report**: the `(PositionTicks, LastPlaybackCheckIn,
   IsPaused)` tuple changed. Between reports, extrapolate at 1.0x.
2. **When did the report happen?** `LastPlaybackCheckIn` is server time.
   `ClockOffset` estimates `server - local` as a running max of lower-bound
   samples: the response `Date` header (truncated to seconds, so always <=
   the true server time) every poll, and `checkin - now` for each report
   that was not there at the previous poll. The max converges from below and
   never jumps the target around. Report time = `checkin - offset`, clamped to
   `(previous poll, now]`.
3. **First sighting** (daemon start / new item): age = `now - report time`,
   capped at 30 s, added to the anchor so a 7 s old report does not start the
   ghost 7 s late.
4. **Jitter vs seek**: a new report within `jitter_tolerance_s` (0.75) of the
   model is blended 50 %; beyond it the model re-anchors and reports a `seek`
   event, which the daemon turns into a forced ghost seek (bypassing the seek
   cooldown).
5. **Pause / resume**: reported immediately by clients; the model freezes /
   re-anchors on the reported position.

`tests/test_watcher.py` simulates a client reporting every 10 s under 0.5 s
polls with a 37.3 s server clock offset and random poll jitter: error < 0.15 s
after the first ~40 s, seeks detected within one poll, no spurious seeks.

## Lockstep policy (lockstep.py)

`drift = ghost_pos - target_pos`

| condition | action |
|---|---|
| target paused, ghost playing | pause (+ seek if > threshold apart) |
| target playing, ghost paused | seek to target, resume |
| ghost buffering / no position yet | nothing (speed 1.0) |
| \|drift\| >= `seek_threshold_s` and cooldown passed, or forced (client seek) | hard seek |
| `deadband_s` < \|drift\| | speed = 1 - clamp(drift / `converge_s`, ±`max_speed_delta`) |
| \|drift\| <= `deadband_s` | speed 1.0 |

A 0.2 s lead becomes speed 0.96 for ~5 s: inaudible (the ghost is muted) and
invisible on the lights.

## Stalls (watcher.py, StallEstimator)

After a seek / start / resume the TV client reports the target position at
once, then sits on a still frame while it buffers (3-5 s on an Apple TV with
Moonfin). The model holds the position for a learned per-kind stall (priors
3.5 / 3.5 / 1.0 s), the daemon pauses the ghost on the matching frame, and the
first report that shows the position advancing both re-anchors exactly and
updates the estimate (`sync.stall_estimates`, persisted). Steady-state
corrections use the median of the last six residuals with a gain limit, so
whole-second or jittery reports do not move the ghost.

## Ghost (ghost.py)

mpv is launched with `--start` at the modelled position plus an EMA of the
observed startup latency, `--hr-seek=yes` (exact seeks), `--focus-on=never`
(never steals focus), on `--fs-screen-name` / `--fs-screen`. Its JSON IPC is
read on a thread (`PeekNamedPipe` polling on Windows - a blocking `ReadFile`
on a synchronous pipe handle would block writes from other threads).
`observe_property` mirrors `time-pos`, `pause`, `speed`, `paused-for-cache`,
`eof-reached`, `core-idle`. `end-file` `reason=quit` means the user closed the
window (sync disables itself); `reason=eof` means the item finished (no
relaunch until the client is clearly not at the end).

## Engine ordering (daemon.py)

- start: launch mpv → first `time-pos` → `engine.start()`  (never sync the desktop)
- stop:  `engine.stop()` → wait until not syncing (≤ 2 s) → kill mpv

## Measuring

`DriftStats` collects `ghost_pos - target` on every tick while playing and
logs `mean|drift| / p95 / max / bias / seeks` every minute; `/status` exposes
the live value and the last summary. Acceptance for a release: p95 ≤ 0.2 s
and zero drift-seeks over a 30-minute episode.

## Route B (future)

An engine that decodes the ghost itself (mpv `--vo=null` + frame callbacks or
ffmpeg), samples zones per entertainment-channel position from CLIP v2, and
streams over DTLS at 50 Hz - no display, no Hue Sync app, Linux/headless,
could run on the Jellyfin server. Everything above the `Engine` interface
stays the same.
