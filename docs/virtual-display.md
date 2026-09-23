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

## Sleep, display timeouts and the lock screen

Windows' *Turn off display after* timeout switches **every** display off,
the virtual one included. Hue Sync's desktop-duplication capture then gets no
frames and the lights sit on one dim colour until any input wakes the
displays. Hue Ghost handles this (`ghost.keep_awake`, Display page > *Keep
displays awake*):

- `playing` (default) - when playback starts it wakes the displays (resets the
  display idle timer + a 1-px mouse jiggle, net zero movement) and then holds
  the display *and* the system awake with `SetThreadExecutionState` until the
  ghost closes. Afterwards the normal timeouts apply again.
- `always` - hold them awake all the time (a dedicated media PC).
- `off` - do nothing.

### Screen care (OLED)

Holding every display awake for a film leaves your real monitor on one static
picture for hours. Display page > *Screen care* (both apply live, both only
while a video ghost plays - music and apps on this PC are left alone):

- `ghost.pixel_shift_min` (default `3`, `0` = off) - every N minutes the
  ghost's picture moves round a 9-point orbit via mpv's `video-pan-x/y`
  (0.3 % of the picture per step: ~6 px at 1080p). The orbit carries on
  across ghosts, so a night of episodes wears every position equally.
- `ghost.blackout_idle_min` (default `0` = off) - once `GetLastInputInfo` says
  nobody has touched the mouse or keyboard for N minutes, every display except
  the ghost display (and the one Hue Sync reports capturing) is covered by a
  borderless, always-on-top, focus-never mpv window playing solid black. Any
  input closes them within a tick. If the ghost display is not set (or not
  plugged in) nothing is covered: there would be no telling which screen the
  lights come from.

Two things Hue Ghost cannot do: wake a PC that has gone to **sleep** (set the
sleep timeout to *Never* on a PC that should light the room unattended), and
capture a **locked** desktop (the lock screen is a secure desktop no app can
read). When the PC is locked while a movie starts, Hue Ghost logs a warning
and the Home page says *PC locked*; sign in and the colours follow.
