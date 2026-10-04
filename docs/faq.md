# Frequently asked questions

Quick answers and fixes. For every setting see [settings.md](settings.md), and for every kind of source see [sources.md](sources.md).

## The lights change a little late or early

Adjust the offset on **Home**. Press **+0.25** if the lights change late on a hard cut, or **-0.25** if they change early. Typical values are 1.0 to 2.0 s. If you switch Time lock on or off, check the offset again.

## The lights sit on one dim colour after the PC was idle

The display went to sleep and Hue Sync lost its picture. Keep **Display > Keep displays awake** on *While the ghost plays*, and switch the PC's **sleep** timeout off.

## A game is missed

*Any game* covers games from the big stores (in any folder), games a launcher started, games Windows lists in its Game Bar, and games in exclusive fullscreen. A source bound to a launcher such as `steam.exe` means the games that launcher starts, not the launcher's own window. For a game none of that recognises, add its `.exe` as its own source (*Sources*, then type the `.exe` or pick it from the running apps).

## Hue Sync stays off

Only one thing can stream to an entertainment area at a time. Stop any other sync first: the Hue app, a Sync Box or Hue Synco music sync.

## Does the ghost show up in Jellyfin's Continue watching?

Continue watching lists only what you watched on the TV. The ghost reads the file directly and leaves Jellyfin's playback records to the TV.

## Does it need the internet?

Hue Ghost runs on your own network. It talks to your Jellyfin server, to the Hue Sync app on the same PC (through the public control setting you switch on) and to its own local control API. It leaves the Hue Bridge to Hue Sync, so your bridge credentials stay inside Hue Sync.

Two Hue Sync settings are changed through Hue Sync's own settings file: the entertainment area and *use audio for effects*. Hue Ghost edits that file while Hue Sync is closed. The details are in [hue-sync-public-control.md](hue-sync-public-control.md).

## Can I use a Mac or Linux PC?

Windows is the supported platform. Hue Sync runs on Windows and macOS, and Hue Ghost also runs from source on macOS (untested). See [development.md](development.md) for running from source.

## What about Jellyfin SyncPlay?

Hue Ghost follows the TV through the Jellyfin server and leaves the TV alone, so it works with any Jellyfin client. SyncPlay is supported by few TV clients, and one buffering member pauses the whole group.

## Where do I find more?

- [sources.md](sources.md): phones, music, several rooms, several apps in one source, game streaming and how *Any game* decides
- [settings.md](settings.md): mode, intensity, timing, Time lock, displays, OLED care and the command line
- [Issues](https://github.com/engabd11/HueGhost/issues): questions and bug reports
