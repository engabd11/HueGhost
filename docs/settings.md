# Settings: how the sync behaves

Everything here is set in the app (Home, Sync and Display pages) and saved in
`%APPDATA%\hue-ghost\config.json`. For the quick version see the
[README](../README.md#features-at-a-glance).

## Mode, intensity and audio

Hue Ghost sets **what Hue Sync does** with the ghost picture on every sync it
starts, so it no longer depends on what the app was last left on:

- **Mode** - Video (reacts to the picture), Music (to the sound) or Games.
- **Intensity** - Subtle / Moderate / High / Extreme.
- **Use audio for effects** - the app's own switch for video and games mode:
  the lights react to the soundtrack as well as the picture. Leave it on
  *App's own* and Hue Ghost never touches it. This is the default: each source
  can override it with its own **Audio** setting on the Sources page.

Mode and intensity go over the Public Control socket and apply live,
mid-movie: switching from Video to Music or Games costs one message and never
restarts the app or drops the sync. The audio switch is the one setting the app
only reads at start-up, so choosing On or Off restarts Hue Sync (~3 s) - applied
in the same restart as an area change, never twice, and written for Video *and*
Games together so that switching between them afterwards stays free.

**The controls follow the Hue Sync app, always.** Mode and intensity on Home,
in the tray, over the API and in Home Assistant show what the app is really
doing - not what is saved here. Change either one in Hue Sync itself and the
buttons move with it; Hue Ghost stops asserting its own choice for the rest of
that session. They keep following it across the restart Hue Ghost performs to
apply an entertainment area, too: while the socket is down the last thing the
app said stands, and the first update after it is back wins. Hue Sync keeps an
intensity per mode, so the one Hue Ghost is set to is re-applied when the mode
changes - only an intensity *you* pick is taken as a new setting.

What gets **saved** is narrower. The settings below are the default for sources
that have not chosen a mode or an intensity of their own, so a change made
while a source that *has* chosen one is playing is kept for the session and no
further: saving it would quietly hand one film's choice to every other source.

All three are on **Home** and on the **Sync** page, and are exposed to Home
Assistant and the CLI. They are the **defaults**: a source with a mode or an
intensity of its own overrides them while it is the one playing, and hands them
back when it stops.

The chosen settings are re-applied whenever the **source** changes, not only
when a new item starts. That matters because the two are not the same thing: an
app on this PC can take the lights over from a phone that is still connected,
and no source ever reports anything "new" at that moment.

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

**Time lock (Sync page, on by default).** Normally every
progress report from the TV nudges the ghost's timeline a little, so the
ghost is always correcting. With *Time lock* on, once the drift reaches 0.0 s
the timeline is held and the ghost simply plays at 1.0x; Home shows
*drift +0.00 s · locked*. Seeking, pausing, buffering - or the TV's
precisely timed reports drifting more than *Release when the TV is off by*
(0.02 s) from the locked timeline - releases it, the usual corrections take
over, and it locks again once playback has settled. It also stops steering by
reports Jellyfin did not time: Moonfin on the Apple TV sends its position
every second but Jellyfin times only every fifth, which otherwise leaves the
ghost ~0.2 s ahead - so re-check your offset after turning it on.

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

**OLED screens** (Display page > *Screen care*): holding the displays awake
for a whole film means hours of one unchanging picture - the desktop on the
screens nobody is looking at, the frame the ghost is paused on - and an OLED
burns that in. Two remedies, both live, both only while the ghost plays:

- *Shift the ghost picture every* N minutes (default **3**, 0 = off) moves the
  ghost's picture a few pixels round a slow orbit. Invisible, and the lights
  cannot tell - Hue Sync averages much larger areas of the screen.
- *Black out the other displays after* N minutes (default **off**) covers
  every display Hue Sync is *not* capturing in black once nobody has touched
  the mouse or keyboard for that long - a black OLED pixel is switched off.
  Any input brings them straight back. The ghost display, and whichever one
  Hue Sync reports capturing, is never covered, so the lights carry on.

## Home Assistant

**Home Assistant** page: switch *Allow control from the LAN* on, generate a
token, save, restart. Then either:

- **[Hue Synco](https://github.com/engabd11/syncoV2)** (HACS): *Configure >
  Hue Ghost host / port / token* gives you a **Hue Ghost** device - a
  **Global sync** light that is the master control (on/off *and* the area's
  level), **sync status / area / active source / now playing** sensors,
  **mode** and **intensity** selects, a **switch per source** named for the
  source itself, a **use audio for effects** switch and the sync-offset number
  - plus a **Hue Ghost card** in this app's own colours that wires itself up.
  Turning movie mode on hands the entertainment area over from music sync
  automatically.
- Plain REST (see [home-assistant.md](home-assistant.md)):
  `POST /on`, `/off`, `/set {"offset_delta": 0.25}`, `GET /status`, all with
  `Authorization: Bearer <token>`. `/set` also takes `{"brightness": 0-100}`
  and `{"binding": {"key": "<id>", "enabled": false}}`; `/status` lists every
  source under `bindings`.

## Command line

The installer also puts `hue-ghost.exe` next to the app (add
`C:\Program Files\Hue Ghost` to PATH, or run it from there):

```
hue-ghost gui [--minimized]    the desktop app (what the Start menu shortcut runs)
hue-ghost run                  headless daemon, logs to the console
hue-ghost doctor               checks Jellyfin, mpv, displays, Hue Sync, the control API
hue-ghost status [--json]      what the running app sees
hue-ghost on | off | toggle    master switch
hue-ghost set --offset-delta 0.25 | --mode music | --intensity high
               --use-audio on|off|app | --brightness 60 | --brightness-step -10
               --enable-source <id> | --disable-source <id>
hue-ghost setup                text-mode setup wizard (headless machines)
hue-ghost install-autostart    start at sign-in (the installer's checkbox does the same)
```

Configuration lives in `%APPDATA%\hue-ghost\config.json` (Settings > *Open
config folder*); every field is editable from the app.
