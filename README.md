# Hue Ghost

**Philips Hue light sync for Jellyfin, PC apps and games.**

Press play on any Jellyfin client (Apple TV, Android TV, a phone and more), or start a game or video on your PC, and the Hue lights in the room follow the picture. Hue Ghost does it with the free Hue Sync desktop app, and your TV keeps playing its own stream, so 4K, HDR and Dolby Vision work as usual.

Free and open source (MIT), built by [Cyborg Automation AU](https://cyborgautomation.com.au/pages/hue-ghost). [Download the latest release](https://github.com/engabd11/HueGhost/releases/latest).

```
TV plays the film (any Jellyfin client)
  → Jellyfin server reports what is playing
  → Hue Ghost on your PC plays a silent copy in step with the TV: the ghost
  → Hue Sync app watches the ghost
  → Hue Bridge → your lights
```

## What it does

The free Philips **Hue Sync** desktop app turns whatever is on a PC screen into light. Hue Ghost extends that to the picture on your TV, and automates the rest:

1. **Films and shows on your TV (Jellyfin).** Hue Ghost plays a silent, hidden copy of the same film on the PC, the **ghost**, and keeps it on the same frame as the TV. Hue Sync watches the ghost, so the lights match the TV.
2. **Apps and games on your PC.** The picture is already on a screen Hue Sync can see, so Hue Ghost switches the sync on when something starts playing, in the right room and mode.
3. **Music.** Hue Ghost plays the track to an audio output you set aside, and Hue Sync's music mode listens to that.

You set it up once. After that you press play, and the lights stop when playback stops.

## What you need

| | |
|---|---|
| **PC** | Windows 10 or 11 (64-bit), left on while you watch, with sleep switched off |
| **Lights** | A Hue Bridge (v2) with an **entertainment area** set up in the Hue app |
| **Hue Sync** | The free [Hue Sync desktop app](https://www.philips-hue.com/en-us/explore-hue/propositions/entertainment/sync-with-pc), paired with your bridge |
| **For TV sync** | A Jellyfin server (10.9 or newer) on your network, and a Jellyfin API key |
| **For TV sync** | A spare display for the ghost: the installer adds a **virtual display**, or use an HDMI dummy plug |
| **For Jellyfin music** *(optional)* | A spare audio output reserved for the ghost, such as an unused HDMI or optical port, or a virtual audio cable |

Syncing apps and games on the PC only? You need just the PC, the bridge and Hue Sync.

The ghost plays the original file, so the graphics card should decode your films (4K HEVC or AV1 needs a reasonably recent GPU).

## Install

1. Download **`HueGhost-Setup-<version>.exe`** from [Releases](https://github.com/engabd11/HueGhost/releases) and run it. It installs the app, the **mpv** player that plays the ghost, and (optional, recommended) the **virtual display**. It can also start Hue Ghost when you sign in.
2. Install **Hue Sync** if it is new to you, pair it with your bridge and pick the entertainment area of the room.

## Set up (about 5 minutes)

Open **Hue Ghost** from the Start menu. The first run opens on **Sources**.

1. **Sources**: add what the lights should follow.
   - *A TV or phone:* enter your Jellyfin URL and API key (Jellyfin Dashboard > API Keys > +), click *Test connection*, play something on the TV, select it and click *Add selected player*.
   - *An app or game on this PC:* use **Quick add** (for example *Any game* or *Web browsers*), pick it from the list of running apps, or type its `.exe`.
   - Give each one its **mode** (Video, Music or Games), **intensity** and **room** (entertainment area), then **Save sources**.
2. **Display**: pick the ghost display (the virtual display) and click *Show test pattern* to check it is the right one. (TV sync only.)
3. **Hue Sync**: in the Hue Sync app, once, switch *Settings > Allow public control* **on** and *Start syncing when Hue Sync launches* **off**. The page checks this for you.
4. **Home**: press play on the TV and watch the state go from *idle* to *ghost playing* to *syncing*.

Closing the window leaves Hue Ghost running in the tray. The tray icon shows the state: grey idle, blue ghost playing, green syncing, red when Hue Sync is unreachable.

## What you can sync

| Source | How to add it | What Hue Sync watches |
|---|---|---|
| Jellyfin on a TV, Apple TV, Android TV or phone | *Sources > Jellyfin* | the ghost |
| Jellyfin music | same, plus a spare output (*Display > Ghost audio output*) | the ghost (sound only) |
| A browser (YouTube, Netflix, Disney+ and more) | *Quick add > Web browsers* | your screen |
| VLC, MPC-HC, PotPlayer, Kodi, Plex, Jellyfin Media Player | *Quick add > Video players* | your screen |
| Spotify | *Quick add > Spotify* | your screen |
| **Any PC game** (Steam, Epic, GOG, Xbox, EA, Ubisoft, Riot) | *Quick add > Any game* | your screen |
| **Games streamed with Sunshine to Moonlight** | *Quick add > Any game* on the Sunshine PC | your screen |
| Games streamed *to* this PC (Moonlight, GeForce NOW, Parsec, Steam Link) | *Quick add > Games streamed to this PC* | your screen |
| One specific app or game | pick it from the running apps, or type its `.exe` | your screen |
| Anything that fills the screen | *Quick add > Any app on this PC* | your screen |

- **One source at a time.** If several are playing, the one **highest in the list** wins, so drag rows to reorder. Each row has an on/off switch.
- **Each source brings its own look.** When a source starts, Hue Sync switches to its mode, intensity and room, so the Apple TV can be *Video / Subtle* in the lounge room and a game *Games / Extreme* in the office.

Phones, music, several rooms, several apps in one source and how *Any game* decides are in [docs/sources.md](docs/sources.md).

## Features at a glance

- **Automatic on and off.** Lights start when playback starts and stop about 1.5 s after it stops. Pauses and next episodes are handled for you.
- **Frame-accurate TV sync.** The ghost follows the TV to within about 0.1 to 0.2 s, and follows seeks within about a second.
- **Time lock** (on by default). Once the ghost matches the TV, Hue Ghost holds it steady instead of correcting on every report, which makes the lights noticeably smoother. It lets go on a seek, pause or buffering, and locks again straight after.
- **Offset tuning from the couch.** If the lights change *late* on a hard cut, press **+0.25** on Home. If *early*, press **-0.25**. Typical values are 1.0 to 2.0 s.
- **Several rooms.** Each source can light its own entertainment area.
- **Displays kept awake.** Hue Ghost holds the displays awake while something plays, so Hue Sync always has a picture to watch.
- **OLED care.** The ghost picture shifts a few pixels now and then, and other screens can go black while you are away from the PC.
- **Home Assistant.** Control it from [Hue Synco](https://github.com/engabd11/syncoV2) or plain REST ([docs/home-assistant.md](docs/home-assistant.md)).
- **Tray icon and command line** (`hue-ghost status`, `on`, `off`, `set`, `doctor` and more).

Every setting is explained in [docs/settings.md](docs/settings.md).

## Runs on your own network

Hue Ghost talks to your Jellyfin server, to the Hue Sync app on the same PC (through the public control setting you switch on) and to its own local control API. Your bridge credentials stay inside Hue Sync. How Hue Sync is driven is described in [docs/hue-sync-public-control.md](docs/hue-sync-public-control.md).

Hue Ghost is an independent project, not affiliated with or endorsed by Signify (Philips Hue) or Jellyfin.

## Part of the Cyborg Automation lighting projects

- **[CAMusic](https://github.com/engabd11/CAMusic)** is an Android music player made for Sendspin players on Music Assistant, with Hue light shows.
- **[Hue Synco](https://github.com/engabd11/syncoV2)** is a Home Assistant integration that lights Hue to the music of any media player, and controls Hue Ghost from its Movie mode.
- **Hue Ghost** lights films, games and apps from your PC.

Use one or all three. They can share an entertainment area by taking turns, because an area streams from one app at a time. See all three on the [open source lighting page](https://cyborgautomation.com.au/pages/oss-lighting).

## Documentation

- [docs/sources.md](docs/sources.md): every kind of source, priority, music, rooms, games and streaming
- [docs/settings.md](docs/settings.md): mode, intensity, timing, time lock, displays, OLED care and the command line
- [docs/home-assistant.md](docs/home-assistant.md): Home Assistant and the REST API
- [docs/virtual-display.md](docs/virtual-display.md): the ghost display
- [docs/architecture.md](docs/architecture.md): how the sync works inside
- [docs/hue-sync-public-control.md](docs/hue-sync-public-control.md): how Hue Sync is driven
- [docs/faq.md](docs/faq.md): common questions and fixes
- [docs/development.md](docs/development.md): running from source, tests, building the installer and the code layout
- [CHANGELOG.md](CHANGELOG.md): what changed in each version

## Credits and licence

Hue Ghost stands on these projects:

- **[mpv](https://mpv.io)** plays the ghost (GPLv2+; bundled by the installer).
- **Philips Hue Sync** (Signify) turns the screen into light.
- **[Jellyfin](https://jellyfin.org)** tells Hue Ghost what the TV is playing (GPLv2).
- **[Virtual Display Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver)** provides the ghost display (MIT; bundled).
- **[Qt](https://www.qt.io) / [PySide6](https://doc.qt.io/qtforpython-6/)** power the desktop app (LGPLv3).
- **[PyInstaller](https://pyinstaller.org)** and **[Inno Setup](https://jrsoftware.org/isinfo.php)** build the installer.
- **[Home Assistant](https://www.home-assistant.io)** and **[Hue Synco](https://github.com/engabd11/syncoV2)** add smart-home control.

Hue Ghost is MIT licensed. The installer bundles mpv (GPLv2+, provenance in `mpv\SOURCE.txt`) and the Virtual Display Driver (MIT). Source for the GPL components is available from their repositories.

Built by **Cyborg Automation AU**, Melbourne: private, local smart-home automation.
