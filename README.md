# Hue Ghost

**A software Hue Sync Box for Jellyfin.** Sync your Philips Hue entertainment
area to whatever your TV is playing from Jellyfin - Apple TV, Android TV,
phone, any client - without buying a Hue Sync Box or Sync Camera.

Built by **Cyborg Automation AU**. MIT licensed. See [Credits](#credits).

```
   TV (Jellyfin client)  --plays-->  Jellyfin server  <--follows session--  Hue Ghost (PC)
                                                                                |
        living-room lights  <-- Hue Bridge <-- Hue Sync app <-- captures -- ghost display
                                                (official, free)           (muted mpv, in lockstep)
```

How it works: the PC keeps a muted **ghost** copy of the same video in
lockstep with the TV, on a display the official **Hue Sync** desktop app is
pointed at. Hue Sync does what it already does well - turn a screen into
light - and Hue Ghost automates the two things you would otherwise do by hand:
*keep the ghost exactly where the TV is* and *start/stop Hue Sync at the right
moments*.

Press play on the TV, the lights follow. Press stop, they stop. No cables to
the TV, no HDMI passthrough, no HDR / 4K / Dolby Vision limitations - the TV
keeps playing exactly as before.

## Install (Windows)

1. Download **`HueGhost-Setup-<version>.exe`** from
   [Releases](https://github.com/engabd11/HueGhost/releases) and run it. The
   wizard installs:
   - the Hue Ghost app (desktop control panel + tray icon + command line),
   - **mpv** (bundled - plays the ghost),
   - a **virtual display** for the ghost (optional, recommended - a signed
     driver by [VirtualDrivers](https://github.com/VirtualDrivers/Virtual-Display-Driver);
     keeps your real screen free while a movie plays),
   - and can start Hue Ghost at sign-in, minimised to the tray.
2. Install the official
   [Hue Sync desktop app](https://www.philips-hue.com/en-us/explore-hue/propositions/entertainment/sync-with-pc)
   if you don't have it, pair it with your bridge and select the
   **entertainment area** of the room where the TV is.

Requirements: Windows 10/11 (64-bit), Jellyfin 10.9+ on the LAN, a Hue Bridge
(v2) with an entertainment area, and a PC that stays on while you watch.
macOS/Linux: the daemon and the app run from source
(`pip install "hue-ghost[gui] @ git+https://github.com/engabd11/HueGhost"`;
Hue Sync for macOS is untested; Linux has no Hue Sync app - see *Limitations*).

## Set up (5 minutes, all in the app)

Launch **Hue Ghost** (Start menu). The first run opens on the **Player** page:

1. **Players** - enter your Jellyfin URL and an API key (Jellyfin Dashboard >
   API Keys > +). Click *Test connection and list players*, play something on
   the TV, select it, *Add selected player*, *Save players*. Add every device
   you want followed (top of the list wins if several play at once).
2. **Display** - pick the ghost display (the virtual display, or a dummy plug)
   and click *Show test pattern* to be sure it's the right one. mpv is detected
   automatically.
3. **Hue Sync** - the page checks the app for you. In Hue Sync, once:
   *Settings > Allow public control* **ON**, *Display* = the ghost display,
   your entertainment area selected, *Start syncing when Hue Sync launches*
   **OFF**.
4. Back on **Home**: press play on the TV and watch the state go
   *idle -> ghost playing -> syncing*.

That's it. Close the window - Hue Ghost keeps running in the tray (the ghost
icon changes colour with the state: grey idle, blue ghost playing, green
syncing, red = Hue Sync unreachable).

## One PC, several rooms

Each player can be bound to a Hue **entertainment area** on the Players page
(`lights:` dropdown). When the Apple TV in the living room plays, Hue Sync
targets the living-room area; when the office TV plays, the office lights -
fully automatic. The Hue Sync app has no API for selecting an area, so Hue
Ghost switches it the only way possible: it stops its sync, restarts the app
silently with the new selection (~3 s) and resumes. That happens only when
playback moves to a device bound to a *different* area, i.e. once per
viewing session, never mid-movie. Players left on "Hue Sync's current area"
don't touch the selection.

## Tuning the timing (from the couch)

Both players must be on the same frame or the lights are wrong. Hue Ghost
measures it: the **Home** page shows the live drift between the ghost and the
TV (green zone = within 0.15 s) and a per-minute quality summary.

The one thing that is yours to tune is the **offset** - how far ahead of the
TV the ghost runs, to cancel the capture -> bridge -> lamp latency and your
TV's own display lag. On **Home** or **Sync**: watch a hard cut; if the lights
change *late*, press **+0.25**; if *early*, **-0.25**; then refine in 0.05 s
steps. It applies instantly and is saved. Typical values: 1.0-2.0 s.

Everything else is automatic: Hue Ghost anchors on the TV's progress reports,
learns how long your TV buffers after a seek (shown under *Sync > Learned TV
buffering*), and nudges the ghost's speed by a few percent instead of jumping.
The **Sync** page exposes the advanced knobs if you want them.

**Stopping** (Sync page): the lights go off **1.5 s** after the TV stops
(*Lights off after the TV stops*); the ghost player itself stays on standby
for 10 s (*Close the ghost after*) so a TV that comes straight back - next
episode, a seek that restarts playback - does not need a fresh launch. *Lights
off when paused for* N minutes (0 = never) covers long pauses.

**Displays that fall asleep** (Display page > *Keep displays awake*): Windows
switches every display off after its idle timeout, the virtual ghost display
included, and Hue Sync then captures nothing - the lights sit on one dim
colour until someone touches the mouse. The default, *While the ghost plays*,
wakes the displays the moment playback starts and holds them awake until it
stops; the PC itself must not be set to sleep (Hue Ghost cannot wake a
sleeping PC). A locked PC cannot be captured either: Hue Ghost says so on the
Home page.

## Home Assistant

**Home Assistant** page: switch *Allow control from the LAN* on, generate a
token, save, restart. Then either:

- **[Hue Synco](https://github.com/engabd11/syncoV2)** (HACS): *Configure >
  Hue Ghost host / port / token* gives you a **Movie mode** switch, a state
  sensor (idle / ghosting / syncing with now-playing and drift attributes), an
  intensity select and the sync-offset number - and turning movie mode on
  hands the entertainment area over from music sync automatically.
- Plain REST (see [docs/home-assistant.md](docs/home-assistant.md)):
  `POST /on`, `/off`, `/set {"offset_delta": 0.25}`, `GET /status`, all with
  `Authorization: Bearer <token>`.

## Command line

The installer also puts `hue-ghost.exe` next to the app (add
`C:\Program Files\Hue Ghost` to PATH, or run it from there):

```
hue-ghost gui [--minimized]    the desktop app (what the Start menu shortcut runs)
hue-ghost run                  headless daemon, logs to the console
hue-ghost doctor               checks Jellyfin, mpv, displays, Hue Sync, the control API
hue-ghost status [--json]      what the running app sees
hue-ghost on | off | toggle    master switch
hue-ghost set --offset-delta 0.25 | --intensity high | --brightness-step -10
hue-ghost setup                text-mode setup wizard (headless machines)
hue-ghost install-autostart    start at sign-in (the installer's checkbox does the same)
```

Configuration lives in `%APPDATA%\hue-ghost\config.json` (Settings > *Open
config folder*); every field is editable from the app.

## How the sync works (short version)

- The TV client reports its position to Jellyfin only every few seconds.
  Hue Ghost anchors **only on new reports**, maps the report's server timestamp
  to local time with a clock-offset estimator, and tells jitter from real
  seeks - so a stale report never causes a correction, and a real seek is
  followed within one poll (0.5 s).
- After a seek, start or resume, TV clients sit on a still frame while they
  buffer (3-5 s on an Apple TV). Hue Ghost holds the ghost for a **learned
  stall** per event kind, then resumes exactly on target.
- The ghost's position is **read back from mpv** (not estimated), and a
  proportional speed controller (+/-4 %) removes small drift invisibly; hard
  seeks only happen for real seeks or > 1 s drift.
- Start/stop order avoids flashing your desktop into the living room: the
  ghost renders first, then Hue Sync starts; Hue Sync stops before the ghost
  window closes.

Details: [docs/architecture.md](docs/architecture.md). The Hue Sync app is
driven over its local "Public Control" WebSocket, documented in
[docs/hue-sync-public-control.md](docs/hue-sync-public-control.md).

## Limitations / FAQ

- **Windows** (installer) / **macOS** (from source, untested) - that's where
  the Hue Sync app runs. A self-contained engine (own colour extraction + DTLS
  entertainment stream, Linux/headless, no display tricks) is the planned
  "Route B" behind the same engine interface.
- **One streamer per entertainment area.** If something else (Hue app scene
  sync, Hue Synco music sync, a Sync Box) is streaming to the area, Hue Sync
  can't start. The Hue Synco integration handles the hand-over.
- **Long pauses?** Optional: the lights go off after the TV is paused for X
  minutes (Sync page > *Lights off when paused for*, 0 = never) and come back
  when you press play.
- **Lights stay on one dim colour after the PC sat idle?** The displays went
  to sleep. Display page > *Keep displays awake* = *While the ghost plays*
  (default) wakes them when a movie starts. Make sure the PC's *sleep* timeout
  is off; the *display* timeout can stay.
- **Accuracy**: about +/-0.1-0.2 s in steady state; a seek on the TV is
  followed within ~1 s plus the TV's own buffering. A Sync Box is ~0.1 s.
  Bias/mood lighting: indistinguishable; frame-critical flashes: close.
- **Continue-watching pollution?** No. The ghost reads the file over
  `/Videos/{id}/stream?static=true` and never reports playback, so Jellyfin
  never sees a second session.
- **Transcoding?** The ghost plays the original file (hardware-decoded); the
  TV can transcode independently. 4K HEVC/AV1 needs a GPU that decodes it.
- **Jellyfin SyncPlay?** Deliberately not used: TV clients rarely support it,
  it needs a group per session, and a buffering peer pauses the whole group -
  Hue Ghost must never be able to stall your TV.
- **Virtual display quirks**: some apps open on the last-used display; Win+P
  and per-app display memory apply as with any second monitor. The installer's
  uninstaller removes the virtual display again.

## Credits

Hue Ghost stands on these projects:

- **[mpv](https://mpv.io)** - plays the ghost: the muted player driven in
  lockstep over its JSON IPC (GPLv2+; bundled by the Windows installer).
- **Philips Hue Sync** - the official desktop app (Signify) that turns the
  ghost display into light. Hue Ghost is not affiliated with or endorsed by
  Signify / Philips Hue.
- **[Jellyfin](https://jellyfin.org)** - the media server whose sessions tell
  Hue Ghost what the TV is watching (GPLv2).
- **[Virtual Display Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver)**
  - the signed virtual display the ghost plays on (MIT; bundled).
- **[Qt](https://www.qt.io) / [PySide6](https://doc.qt.io/qtforpython-6/)** -
  the desktop app (LGPLv3).
- **[PyInstaller](https://pyinstaller.org)** and **[Inno Setup](https://jrsoftware.org/isinfo.php)**
  - the Windows build and the installer wizard.
- **[Home Assistant](https://www.home-assistant.io)** and the
  **[Hue Synco](https://github.com/engabd11/syncoV2)** integration - movie
  mode from your smart home, with hand-over from music sync.

Built by **Cyborg Automation AU** - Melbourne, private and local smart-home
automation.

## Changelog

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

## Compliance & privacy

- **No cloud, no telemetry.** Hue Ghost talks to exactly three things: your
  Jellyfin server, the Hue Sync desktop app on the same machine (its local
  Public Control WebSocket, which *you* enable in the app), and its own local
  API on `127.0.0.1`. Nothing else. Bridge credentials never pass through
  Hue Ghost — they stay inside the Hue Sync app.
- **Philips Hue API.** Hue Ghost never talks to the Hue Bridge directly:
  pairing, the entertainment session and the DTLS stream are handled by the
  official Hue Sync app — the certified path. The Public Control WebSocket is
  an undocumented but user-opt-in interface of that app, and switching
  entertainment areas writes the app's own `bridge.json` selection while it
  is closed. Both are documented in
  [docs/hue-sync-public-control.md](docs/hue-sync-public-control.md) and may
  change across Hue Sync versions.
- **Trademark.** Hue Ghost is an independent project — not affiliated with,
  endorsed by, or certified by Signify / Philips Hue or Jellyfin.
- **Licenses.** Hue Ghost is MIT. The Windows installer bundles
  [mpv](https://github.com/mpv-player/mpv) (GPLv2+; the exact build's
  provenance ships as `mpv\SOURCE.txt` inside the installer) and the
  [Virtual Display Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver)
  (MIT). Corresponding source for the GPL components is available from those
  repositories.

## Development

```powershell
pip install -e ".[dev,gui]"
python -m pytest -q                       # unit tests + mock Hue Sync WebSocket + API tests
python -m hueghost gui                    # the app from source
python -m hueghost doctor
powershell -File scripts\build_installer.ps1   # dist\installer\HueGhost-Setup-<ver>.exe
```

Layout: `hueghost/gui/` (PySide6 app), `daemon.py` (orchestration),
`watcher.py` (position model), `lockstep.py` (pure policy), `ghost.py` (mpv +
IPC readback), `engines/huesync.py` (Hue Sync Public Control), `control.py` +
`webapi.py` (HTTP API), `cli.py`, `installer/` (PyInstaller spec + Inno Setup).

MIT licensed — see [Compliance & privacy](#compliance--privacy) for bundling,
trademark and privacy notes.
