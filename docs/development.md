# Development

Run Hue Ghost from source, run the tests and build the installer. Python 3.10 or newer.

```powershell
pip install -e ".[dev,gui]"
python -m pytest -q                       # unit tests + mock Hue Sync + API tests
python -m hueghost gui                    # the app from source
python -m hueghost doctor
powershell -File scripts\build_installer.ps1   # dist\installer\HueGhost-Setup-<ver>.exe
```

The optional extras in `pyproject.toml` are `gui` (PySide6), `tray` (pystray and Pillow), `dev` (pytest) and `build` (PyInstaller plus the GUI and tray packages).

## Code layout

| Path | What it does |
|---|---|
| `hueghost/gui/` | The desktop app |
| `hueghost/daemon.py` | Orchestration |
| `hueghost/sources/` | Jellyfin and this PC |
| `hueghost/pcwatch.py` | PC playback detection |
| `hueghost/presets.py` | Quick add |
| `hueghost/watcher.py` | Position model |
| `hueghost/lockstep.py` | Sync policy |
| `hueghost/ghost.py` | The mpv ghost |
| `hueghost/screencare.py` | OLED care |
| `hueghost/engines/huesync.py` | Driving the Hue Sync app |
| `hueghost/control.py` and `hueghost/webapi.py` | The HTTP control API |
| `hueghost/cli.py` | The `hue-ghost` command |
| `installer/` | The Inno Setup installer |

For how the pieces work together, see [architecture.md](architecture.md).
