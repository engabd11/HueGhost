"""Ready-made PC sources for the Sources page's *Quick add*.

Pure data. Each preset is a partial binding (``exe`` may list several
executables, comma separated) with a one-line hint for the app to show. The
mode and the detection signal are the ones that suit that kind of app, so a
preset works the moment it is added; every field stays editable on its row.

Store (UWP) apps such as the Netflix or Disney+ apps are left out on purpose:
their window belongs to ApplicationFrameHost.exe, not to the app, so there is
nothing reliable to match. A browser, or "Any app on this PC", covers them.
"""
from __future__ import annotations

PRESETS: list[dict] = [
    {
        "name": "Any game",
        "exe": "@games",
        "mode": "games",
        "detect": "fullscreen",
        "hint": "Every game: anything Steam, Epic, GOG, Xbox, EA, Ubisoft or Riot installed or started, "
                "anything Windows lists as a game (wherever it is installed), plus anything running "
                "exclusive fullscreen. Covers games Sunshine starts for Moonlight too.",
    },
    {
        "name": "Steam games",
        "exe": "steam.exe",
        "mode": "games",
        "detect": "either",
        "hint": "Any game you start from Steam, from every Steam library. The Steam window itself "
                "never counts.",
    },
    {
        "name": "Epic, GOG and EA games",
        "exe": "epicgameslauncher.exe,galaxyclient.exe,eadesktop.exe",
        "mode": "games",
        "detect": "either",
        "hint": "Any game started from the Epic Games Launcher, GOG Galaxy or the EA app. The "
                "launchers' own windows never count.",
    },
    {
        "name": "Web browsers",
        "exe": "chrome.exe,msedge.exe,firefox.exe,brave.exe,opera.exe,vivaldi.exe",
        "mode": "video",
        "detect": "fullscreen",
        "hint": "YouTube, Netflix, Disney+ and the rest - when the video is full screen. "
                "(Browser sound alone is not used: a call or a notification would count.)",
    },
    {
        "name": "Video players",
        "exe": "vlc.exe,mpc-hc64.exe,mpc-hc.exe,mpc-be64.exe,potplayermini64.exe,kodi.exe,"
               "plex.exe,jellyfinmediaplayer.exe",
        "mode": "video",
        "detect": "either",
        "hint": "VLC, MPC-HC/BE, PotPlayer, Kodi, Plex and Jellyfin Media Player on this PC.",
    },
    {
        "name": "Spotify",
        "exe": "spotify.exe",
        "mode": "music",
        "detect": "audio",
        "hint": "Music mode: the lights follow what Spotify plays on this PC.",
    },
    {
        "name": "Games streamed to this PC",
        "exe": "moonlight.exe,geforcenow.exe,parsecd.exe,streaming_client.exe",
        "mode": "games",
        "detect": "fullscreen",
        "hint": "Moonlight, GeForce NOW, Parsec and Steam Link, when they fill the screen.",
    },
    {
        "name": "Any app on this PC",
        "exe": "*",
        "mode": "video",
        "detect": "fullscreen",
        "hint": "Whatever fills a screen - a video, a player, a game. The desktop, the taskbar, the "
                "lock screen and Hue Ghost's own windows never count.",
    },
]
