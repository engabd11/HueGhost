# Changelog

All notable changes to Hue Ghost. The newest release is first.

- **2.11.0** - **More apps and games, less setup.** A new **Any game** source
  follows every game installed through Steam, Epic, GOG, Xbox / Game Pass, EA,
  Ubisoft or Riot - and anything Windows reports as exclusive fullscreen - with
  no per-game setup. That includes games Sunshine starts for a Moonlight client.
  One source can now cover **several apps** (`chrome.exe,msedge.exe,firefox.exe`),
  and the Sources page has **Quick add** presets: any game, web browsers, video
  players, Spotify, games streamed to this PC (Moonlight, GeForce NOW, Parsec,
  Steam Link) and any app. **Time lock is on by default** and no longer
  experimental: it is switched on once for existing installs; switch it off on
  the Sync page and that choice sticks (re-check your offset after the change).
  The README was rewritten to explain simply what Hue Ghost does and needs; the
  detail moved to `docs/sources.md`, `docs/settings.md` and this changelog.
- **2.10.3** - **A player left paused no longer keeps everyone else dark.** The
  first player in your list that had anything loaded won, paused or not - so
  an Apple TV paused on an episode kept the lights (switched off for the
  pause) while the S23 played a film further down the list, and nothing
  synced. A paused player now keeps its place for the same time its lights
  stay on while paused (`sync.pause_stop_min`, `sync.music_pause_stop_s`),
  then gives way to one that is really playing; it takes the lights straight
  back when it plays again. Pause limit 0 = it never gives way.
- **2.10.2** - **Only one Hue Ghost runs at a time.** Double-clicking the app
  while autostart already had it in the tray started a SECOND copy: Home
  Assistant talked to one (it owned port 8787), the window you saw belonged to
  the other, so HA said off/idle while the app said on/syncing - and each
  copy's shutdown closed the other's ghost, which it read as "closed by user"
  and switched sync off by itself. A second launch now brings the running
  copy's window up and exits; the control port is bound exclusively, so two
  daemons can no longer both claim it (`HUEGHOST_INSTANCE` names a separate
  lock for a deliberate dev instance). Pairs with Hue Synco 1.61.0.
- **2.10.1** - **The window no longer freezes ("not responding").** Its
  twice-a-second status refresh, and every Save, waited on the daemon's lock,
  which the daemon holds across a Jellyfin request or an mpv launch - a slow
  one froze the window until Windows closed it as not responding (Windows'
  AppHang reports, and nothing in the log). Both now run on worker threads; with
  the lock held for 4 s the window's longest stall measured 0.08 s. Uncaught
  errors are logged, a native crash or a window unresponsive for 4 s writes
  every thread's stack to `crash.log`, and a daemon step holding the lock over
  2 s is logged by name. **A switched-off source no longer syncs.** A binding
  with a device id matches that id only; the name is a fallback for a
  regenerated id, never while the device itself is connected and never for a
  device another binding owns, switched off or not ('Android S23' used to
  follow CAMusic-linux, also called "Android", whose own binding was off).
  **A paused client no longer lights up at start-up**: found already paused, it
  waits for play (2.9.0 launched, started the sync, and switched it off again
  15 s later). **Any app on this PC**: a new source for whatever fills a screen
  or plays sound; `huesync.exe` + Fullscreen now means the same, so a video
  full screen on the main display is picked up. Adding a duplicate by name, or
  a list entry without a device id, is refused with a message.
- **2.10.0** - **Time lock (experimental, off by default).** Live data from an
  Apple TV (Moonfin) showed the ghost correcting its speed on ~70 % of ticks:
  every 1 s report moved the model's timeline by 0.01-0.15 s, because Jellyfin
  only moves the check-in every 5 s and those reports were timed from the stale
  check-in (~0.2 s lead). The new *Time lock* switch (Sync page, `/set
  {"time_lock": true}`) steers only by the reports Jellyfin timed and, once the
  drift reaches 0, holds the timeline instead of chasing each report; seeks,
  pauses, buffering or timed reports more than 0.02 s off release it.
  Replaying 15 min of recorded Apple TV reports with 5 seeks: mean error 0.20
  -> 0.02 s, p95 0.31 -> 0.06 s, timeline steps 667 -> 6. `/status` gains `time_lock`, the
  per-minute log line `locked N/M ticks`. Fixes: saving any setting while
  something played (even an offset nudge) pushed the global mode and intensity
  over the playing source's own - a phone playing music flipped to video; a
  mode or intensity picked on Home / `/set` while a source with its own value
  played became the global default (now: live for the session only); a music
  ghost volume of 0 % played at 100 %; the Home Assistant REST switch example
  posted to `/status`. Home shows the ghost's *parked* / *closes in N s* /
  *paused* state, the Sync page the Jellyfin clock offset and report count.
- **2.9.0** - **Music that changes track no longer blinks the lights.** Skipping
  a song on a music source used to tear the whole session down - stop the
  sync, kill the ghost, launch a new one, start the sync again - so the lights
  went dark for a second between songs. The music ghost is invisible and the
  lights ride on the audio endpoint, not on a picture, so a track change now
  swaps the file the live ghost plays (`loadfile`) and the sync simply
  continues; the ghost lands on the new track's anchor with a forced seek. The
  same for the moment the ghost itself reaches the end of a track: the engine
  stays on until the client reports the next one (or the idle path stops the
  lights, when nothing follows). Also fixed: two ways a music session could
  take 10+ seconds to start. A client that finished a song sits at its last
  position, and launching a ghost there sent mpv past the end of the file,
  where it died rc=2 in a loop - launches now refuse positions at/after the
  end of the track and clamp the start inside it. And when the configured
  ghost audio output is missing (it rides on its display; asleep, it is gone),
  mpv died the same way - the daemon now checks the endpoint first, wakes the
  display to bring it back, and launches only once it is there. A ghost that
  ran for 30 s or more no longer counts against the launch rate guard, so one
  rough patch of deaths cannot block the next track for minutes.
- **2.8.1** - **The Home controls follow the Hue Sync app.** With a mode and an
  intensity per source, the saved settings became a *default* - but Home, the
  tray, the CLI and Home Assistant still read them, so the buttons sat on the
  previous session's choice while the app reported something else entirely
  (`mode video · intensity subtle` next to **Music** and **High** lit up). They
  report what the engine is really set to now, falling back to the saved
  default only for a mode Hue Ghost has no button for (Hue Sync's "scenes") or
  before the app has said anything at all - so they also hold steady through
  the restart that applies an entertainment area. `/status` gains
  `mode_default` and `intensity_default`. Related: a mode or intensity picked
  in the Hue Sync app is no longer saved over the global default while a source
  that sets its own is playing - that is how a config ends up defaulting to
  something nobody chose.
- **2.8.0** - **Every source brings its own settings, and hand-overs work.**
  Each row on the Sources page now carries a **mode** and an **intensity** as
  well as an area, applied the moment it starts playing: *Apple TV -> Video,
  Subtle*, *phone running a music app -> Music, High*. They were only ever
  applied when a source reported a **new item**, which is not the same thing as
  a source **winning**: an app on this PC taking the lights over from a client
  that was still connected left Hue Sync in the previous source's mode and
  area for the whole session. Two things fell out of the same gap: the losing
  client's **ghost was never closed** (only the idle timer closed one, and it
  does not run while something else plays), so an mpv could be left playing to
  nobody for hours - it is now parked and closed like any other idle ghost; and
  **paused music waited on standby** instead of stopping,
  so a phone that stopped a track kept the source busy until it was switched
  off by hand. Music now stops outright after its 15 seconds - lights off,
  ghost closed - and starts again on the next track.
- **2.7.0** - **Screen care for OLEDs.** Keeping the displays awake so Hue
  Sync can capture the ghost also meant hours of one static desktop on your
  real monitor. The ghost's picture now shifts a few pixels every 3 minutes
  (invisible, and the lights cannot tell), and *Black out the other displays
  after* N idle minutes covers every display Hue Sync does not capture in black
  until the mouse or keyboard is touched. Both are on the Display page, apply
  live, show under `system.screen_care` in `/status`, and can be set over
  `/set` (`pixel_shift_min`, `blackout_idle_min`).
- **2.6.0** - **Phones stop being a guessing game.** Jellyfin calls most of
  them "Android", so two handsets looked identical in the list and one could
  quietly answer for the other. Entries now show the app and the user (and a
  piece of the device id when even that repeats), every source has a **name you
  can edit**, and adding one pins its exact device id instead of its name. Since
  Jellyfin issues an id per app, **one phone can hold a separate configuration
  per app** - music lighting one room, films another. The discovery list gets a
  **Refresh** button, shows what is playing or when a device was last seen, and
  no longer lists the same app twice. Apps on this PC are listed by their real
  names (*Google Chrome*, not chrome.exe) with a search box. **Music now stops
  properly:** a track paused for 15 seconds turns the lights off instead of
  waiting on a background session that may never close. And the app explains
  itself - what the ghost is and why it exists, what each setting does, and
  which single one restarts Hue Sync.
- **2.5.0** - **Hue Sync stops restarting mid-movie.** Changing the mode from
  Video to Music or Games while the lights are running no longer restarts the
  Hue Sync app: the mode has always had a live command, and Hue Ghost was
  needlessly re-deriving the app's start-up-only settings every time it changed.
  Everything those settings need is now applied once, before sync starts - and
  the audio switch is written for Video *and* Games together, so moving between
  them later costs nothing. **Fixed:** "let the app choose" for the capture
  display or the music input could never be satisfied, so it restarted Hue Sync
  on *every* mode change, forever. **Hue Ghost now mirrors the app:** change the
  mode or intensity in Hue Sync itself and Hue Ghost adopts and saves it instead
  of putting its own back two seconds later - window, tray, API and Home
  Assistant included.
- **2.4.0** - **The lights follow more than a TV.** Add an app on this PC - a
  browser playing YouTube, a player, a game - and it lights its own
  entertainment area with no ghost involved, because Hue Sync can capture your
  real screen directly. **Jellyfin music** is followed and synced (the ghost
  plays the track into an output you cannot hear, which is what Hue Sync's
  music mode then listens to); music playing on the PC itself needs nothing but
  a binding. Every source has an **on/off switch**, so one can be ignored
  without being deleted. **Home Assistant** gets an area-brightness light and a
  switch per source. The window opens big enough for its own content and
  remembers its size. Brightness is a real slider, and setting it while Hue
  Sync is closed no longer fails.
- **2.3.0** - Hue Sync's **mode** (Video / Music / Games) and its **use audio
  for effects** switch are Hue Ghost settings now, next to the intensity - on
  Home, on Sync, over the API and in Home Assistant. **Fixed:** changing the
  entertainment area in the Hue Sync app while Hue Ghost was idle made Hue
  Ghost restart the app to put its own area back, which looked like Hue Sync
  crashing; the area (and the audio switch) are now only applied while Hue
  Ghost is starting a sync, once per session, and a change you make in the app
  stands.
- **2.2.0** - Lights go off ~2 s after the TV stops (two-stage stop: the ghost
  stays on standby for 10 s and re-lights instantly if the TV comes back).
  Wakes and holds the displays awake while the ghost plays, so a PC that sat
  idle no longer streams one dim colour until the mouse is touched; warns when
  the PC is locked. New look: warm Cyborg Automation AU theme with Fluent
  icons, artwork on Home, aligned settings, colour-coded log, credits. Hue Sync
  is launched `-silent`; drift stats ignore the second after a seek.
- **2.1.2** - Light engine watchdog (a crashed worker is rebuilt, sync and
  intensity re-asserted); pause timeout option.
- **2.1.0** - Players bound to entertainment areas; Hue Ghost only stops syncs
  it started.
- **2.0.0** - Desktop app, settings API, Windows installer with mpv and the
  virtual display.
