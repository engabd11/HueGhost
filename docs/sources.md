# Sources: what the lights can follow

The **Sources** page is one ordered list of everything that can drive the
lights. This page explains how each kind works and how Hue Ghost picks between
them. For the quick version, see the [README](../README.md#what-you-can-sync).

Two kinds of thing go in the list:

- **A Jellyfin client** - a TV, an Apple TV, a phone. The picture is somewhere
  Hue Sync cannot see, so Hue Ghost mirrors it with the ghost, as always.
- **An app on this PC** - a browser playing YouTube, VLC, Plex's desktop app, a
  game. The picture is *already* on a screen Hue Sync can capture, so there is
  no ghost at all: Hue Ghost simply points the app at your real monitor and
  starts the sync. Pick the app from the list of what is running (what is in
  front and what is making sound come first), or type its `.exe` for a game you
  have not launched yet. Apps are listed by the name they are actually known
  by - *Google Chrome*, not `chrome.exe` - with a search box, because a PC runs
  a hundred processes and about four of them are things you would ever sync.

## Phones, and why one device can appear twice

Jellyfin reports most phones under a generic device name: two different
handsets both arrive as plain **"Android"**, which is no use when you have to
pick one. Each entry therefore shows the **app** and the **signed-in user** too,
and a fragment of the device id when even those are identical - so
*Android - CAMusic (2)* and *Android - CAMusic (1)* are finally two
different things. Every source also has a **name you can edit**: call it
*S23 music* and be done with it.

Jellyfin issues a **separate device id per app**, which is what lets one phone
hold *two* configurations - its music app lighting the bedroom, its video app
lighting the living room - with no switching back and forth. Adding a player
from the list pins that exact id and nothing else; the older behaviour also
stored the device *name*, which is precisely how one phone ended up answering
for another. Matching by name is still there for a TV that gets a new id when
its app is reinstalled, but it follows **every** device that matches, so it is
the wrong tool for a phone.

Each one gets an **on/off switch** - ignore a player for a while without
deleting it - and its own **mode**, **intensity** and **entertainment area**.
That is the whole point of the list: set a source up once and then just play
something. The Apple TV can be *Video / Subtle* in the living room while the
phone running your music app is *Music / High* in the office, and each of them
puts Hue Sync into its own settings the moment it starts. Leave a row on
*Automatic*, *Default* or *Current area* and Hue Ghost does not touch that
choice at all.

Only one thing syncs at a time (Hue Sync has a single area, mode and capture
display), so when several are playing, **the one highest in the list wins**;
drag a row to change that. When the winner changes - an app on this PC starting
while a film is still open on a phone, say - the new source's settings are
applied at once, and the ghost the old source left behind is parked and closed
10 seconds later. Parked, not killed: a source that has the lights for a moment
must not cost a relaunch and a re-sync, so a client that gets them straight back
picks its own ghost up where it left it.

*Automatic* mode, which only a Jellyfin client can have, means "follow the
media": music mode for a song, video for anything else. A PC app has to say
what it is, because nothing else can tell a film from a game. For a PC app you
also choose how Hue Ghost can tell it is playing: **Sound** (it is making
some), **Fullscreen** (it is the window you are looking at, full screen) or
either. Video defaults to sound, games to fullscreen. Nothing is scanned or
guessed - only the executables you actually add are ever looked at, and an
install with no PC sources does no detection work at all.

**Any app on this PC** (Sources > *Add "any app on this PC"*) is the one
exception, on purpose: whatever fills a screen - a video in a browser, a
player, a game - or, with Detect set to Sound, anything the PC plays. The
desktop, the taskbar, the lock screen and Hue Ghost's own windows never count.
A source bound to `huesync.exe` means the same for the window signal (Hue
Sync's own window is never a fullscreen video, and it is how "this PC" was
spelled before).

## Several apps in one source

The `.exe` box takes a comma-separated list, so one row can stand for a group:
`chrome.exe, msedge.exe, firefox.exe` for "any browser", or a game plus its
launcher. The row behaves exactly like a single app - one mode, one intensity,
one area - and plays when any of its apps does.

## Quick add presets

*Sources > Add an app on this PC > Quick add* adds a ready-made row with the
right mode and detection already chosen:

| Preset | Covers | Mode | Detect |
|---|---|---|---|
| Any game | every game in a game library, or exclusive fullscreen | Games | Fullscreen |
| Web browsers | Chrome, Edge, Firefox, Brave, Opera, Vivaldi | Video | Fullscreen |
| Video players | VLC, MPC-HC/BE, PotPlayer, Kodi, Plex, Jellyfin Media Player | Video | Either |
| Spotify | Spotify | Music | Sound |
| Games streamed to this PC | Moonlight, GeForce NOW, Parsec, Steam Link | Games | Fullscreen |
| Any app on this PC | anything full screen | Video | Fullscreen |

Browsers use *Fullscreen* rather than *Sound* because a browser makes sound
for plenty of things that are not a film - a call, a notification. Store apps
such as the Netflix or Disney+ apps are not listed: their window belongs to a
Windows host process, not to the app, so there is nothing reliable to match -
use a browser or *Any app on this PC* for them.

## Any game

The **Any game** source (`@games`) plays when the window in front, filling its
screen, is:

- a program installed in a game library: Steam (`steamapps\common`), Epic
  Games, GOG, Xbox / Game Pass (`XboxGames`), EA, Ubisoft, Riot. The launchers
  themselves (Steam, the Epic launcher, the Riot client) never count; or
- any program while Windows reports **exclusive fullscreen** Direct3D - which
  catches games installed anywhere else (Battle.net, a custom folder).

Borderless-windowed games outside a known library are not detected by Any game;
add their `.exe` as its own source. Nothing is scanned: the path of the window
in front is the only thing looked at, and only while an Any game source exists.

## Game streaming (Sunshine / Moonlight)

**Streaming from this PC** (Sunshine is the host, you play on a TV through
Moonlight): the game runs here, on a display of this PC, so Hue Sync can see
it. Add **Any game** (or the game's own `.exe`) - no ghost, nothing else. If
Sunshine streams from a virtual display, Hue Ghost points Hue Sync at whichever
display the game window is on, as it does for any PC app - nothing to set.

**Streaming to this PC** (Moonlight, GeForce NOW, Parsec or Steam Link runs
here, the game runs elsewhere): add the **Games streamed to this PC** preset.
The picture is on this PC's screen, so Hue Sync captures it like any game.

The lights react to the picture on the PC, a fraction of a second before the
stream reaches the TV. That is usually invisible; if not, lower Hue Sync's own
intensity, or use the Moonlight client's lowest-latency settings.

## Music (Jellyfin)

Jellyfin music used to be invisible: the watcher dropped everything that was
not video, so an album playing on the TV never even reached *Now Playing*. It
is followed like a film now, and it can drive the lights too - but the ghost
has to do the opposite of its usual job. It plays the track's **sound** (no
window at all) into an output **you cannot hear**, and Hue Sync runs in music
mode against that same output. Choose it under *Display > Ghost audio output*:
a spare HDMI or optical port with nothing plugged in, or a virtual audio cable.
Leave it empty and music is shown but not synced — the alternative would be
playing your TV's music out of the PC's speakers.

Music playing **on the PC itself** needs none of that: the sound is already
here, so Hue Sync just listens to its own output.

Music also **stops differently**. A film is paused to be come back to, so the
lights wait minutes for it and the ghost waits with them, parked on standby.
A phone that stops a track usually just leaves the session open in the
background instead of closing it, and waiting for that to disappear means the
lights stay on long after the music ended - so music has its own, much shorter
limit: **15 seconds paused and the sync stops outright**. Not standby: the
lights go off, the ghost is closed and the source stops looking busy, because
nothing is coming back to a paused song. The next track starts it again from
scratch. Both limits are on the *Sync* page.

## One PC, several rooms

When the Apple TV in the living room plays, Hue Sync targets the living-room
area; when the office TV plays, the office lights; when a game starts, your
gaming area - fully automatic. The Hue Sync app has no API for any of this, so
Hue Ghost switches it the only way possible: it stops its sync, restarts the
app silently with the new selection (~3 s) and resumes. The entertainment area,
the capture display, the music input and the audio switch are all applied in
that **one** restart, once per viewing session.

**The app stays yours.** That restart only ever happens while Hue Ghost is
actually starting a sync, and only once per viewing session. Change the area
in the Hue Sync app yourself - while nothing is playing, or in the middle of a
movie - and Hue Ghost adopts it instead of putting its own choice back; it
re-asserts the player's area at the next sync. Players left on "Hue Sync's
current area" never touch the selection at all, and *Hue Sync > Select the
entertainment area for me* turns the whole thing off.
