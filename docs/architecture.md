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
4. **Jitter vs seek**: a new report within `jitter_tolerance_s` (1.5) of the
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

### Time lock (experimental, `sync.time_lock`, off by default)

Live Moonfin data showed the target itself wandering: every 1 s report
re-anchored the model by 0.01-0.15 s and the ghost chased each step (nudging on
~70 % of ticks). Two parts, both switched on by the one setting:

1. **Timed reports only** (`SessionWatcher.precise_timing`): Moonfin reports
   its position every second but Jellyfin moves `LastPlaybackCheckIn` only
   every ~5 s. Aged from the stale check-in, 4 of 5 reports were clamped to the
   previous poll (the model ran ~0.2 s ahead of the TV). Timing them at the
   middle of the poll window is unbiased on average, but 1 s reports against
   0.5 s polls are phase-locked, so that error wanders +/-0.25 s over tens of
   seconds instead of averaging out - found live, it kept releasing the lock.
   So a report whose check-in did not move (`timed=False`) may still raise
   `seek`/`pause`/`resume`, but never steers; timed reports (scatter ~4 ms
   live) steer with the median of the last three, in full.
2. **Lock** (`PlaybackModel.lock`): while armed the deadband is closed so the
   ghost converges to 0; once |drift| <= 0.02 s, the ghost plays at 1.0x, the
   client has sent 3 quiet timed reports and they agree with the timeline to
   0.02 s (`PlaybackModel.settled`), the model stops re-anchoring. Timed reports
   are still measured against the locked timeline; a median gap above
   `time_lock_release_s` (0.02 s) raises `unlock` and the corrections resume.
   Seek / pause / resume / stall events release it too.

Why the release is 0.02 s and not larger: a lock cannot tell a real
correction from noise, and the clock-offset estimate keeps tightening for
minutes after the first lock - a 0.2 s guard held a 0.07-0.1 s bias. With the
noise gone from the steering, 0.02 s costs nothing. Replaying 15 min of
recorded Apple TV polls (5 seeks), scored against the line through the timed
reports: today mean 0.20 s / p95 0.31 s / 667 timeline steps > 10 ms; time
lock mean 0.02 s / p95 0.06 s / 6 steps, 4 release-and-relock cycles.
`/status` carries `time_lock.{enabled,engaged,residual_s}`; the minute summary
adds `locked N/M ticks`.

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

## Players and areas (config.players(), watcher.PlayerSet)

`jellyfin.follow` (+ `follow_area_id`) is player #1; `jellyfin.players` adds
more, in priority order, each with an optional `area_id`. `PlayerSet.pick`
returns the first player that is playing a video (else the first one seen),
and the model resets when the playing device changes. On `new_item` the
daemon calls `engine.set_area(player.area_id)`; `HueSyncEngine` compares it
with `SelectedGroup` from `bridge.json` and, if different, stops its own sync,
kills the app, patches the file and relaunches `HueSync.exe -silent`; the
normal reconnect + reconcile then starts the sync on the new area. The engine
only stops syncs it started itself.

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
