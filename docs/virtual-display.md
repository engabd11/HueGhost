# The ghost display

Hue Sync captures a whole **display**, not a window, so the ghost needs a
display of its own if you want to keep using the PC while a movie plays.
Three options, best first.

## 1. Virtual Display Driver (free, no hardware)

[VirtualDrivers/Virtual-Display-Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver)
(IddSampleDriver-based, signed releases) adds a virtual monitor Windows
treats like a real one.

1. Download the latest `Virtual.Display.Driver-*-setup-x64.exe` from the
   Releases page and install it (UAC prompt).
2. Edit its `vdd_settings.xml` (path shown by the installer, typically
   `C:\VirtualDisplayDriver\`) to a single small mode - **1280x720 @ 60** is
   plenty (Hue Sync samples at low resolution anyway; smaller = less GPU).
   Apply/restart the driver from its tray tool.
3. Windows Settings > System > Display: **Extend**, keep your real monitor as
   the main display, drag the virtual one somewhere out of the way.
4. `hue-ghost doctor` now lists it, e.g. `display 1: \\.\DISPLAY3 1280x720`.
   Put that name in `ghost.screen_name` (or rerun `hue-ghost setup`).
5. Hue Sync > **Display** > pick the virtual display.

Known quirks: some games/apps open on the "last used" display - the virtual
one after the ghost ran there. Windows' "Win+P" and per-app display memory
apply as with any second monitor.

## 2. HDMI / DisplayPort dummy plug (~$8, most robust)

A dummy plug in a spare GPU port is a real second display to Windows and to
Hue Sync. No driver, survives every Windows update. Same steps 3-5 as above.

## 3. No second display

Set `ghost.screen_name` to your main display (or leave both `screen_name` and
`screen_index` empty). The ghost goes fullscreen there while the TV plays,
Hue Sync samples it, and the PC is effectively busy for the movie. Press
**m** to minimise it (the lights then follow your desktop until you restore
it) or **Esc** to close it (sync stops until `hue-ghost on`).

## Colour notes

- HDR content is tone-mapped to SDR by mpv on the ghost display; Hue Sync's
  own colour processing then applies. If colours look washed out, try
  `"extra_args": ["--tone-mapping=bt.2390"]` (or `hable`) in `ghost`.
- Hue Sync's *video* intensity presets (subtle → extreme) control how
  aggressively the lights follow; hue-ghost sets `engine.huesync.intensity`
  when sync starts.
