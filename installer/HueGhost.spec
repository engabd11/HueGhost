# PyInstaller spec: one folder (dist/HueGhost) with two executables sharing _internal:
#   HueGhost.exe   windowed - the desktop app (daemon + control panel + tray)
#   hue-ghost.exe  console  - the CLI (run / setup / doctor / status / on / off ...)
# Build:  python -m PyInstaller installer/HueGhost.spec --noconfirm
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))
ENTRY = os.path.join(ROOT, "scripts", "hueghost_entry.py")
ICON = os.path.join(ROOT, "installer", "hueghost.ico")

hidden = collect_submodules("hueghost") + ["pystray._win32", "PIL.Image"]
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebView",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml", "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtLocation", "PySide6.QtPositioning",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtXml", "PySide6.QtSvgWidgets", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPrintSupport", "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtHelp",
    "PySide6.QtGraphs", "PySide6.QtGraphsWidgets", "PySide6.QtHttpServer", "PySide6.QtWebSockets", "PySide6.QtWebChannel",
    "PySide6.QtSpatialAudio", "PySide6.QtAsyncio", "PySide6.QtConcurrent", "PySide6.QtDBus", "PySide6.QtNetworkAuth",
    "tkinter", "unittest", "pydoc", "doctest",
]

# mpv input bindings: the ghost's, and screen care's black cover
DATAS = [(os.path.join(ROOT, "hueghost", name), "hueghost") for name in ("ghost_input.conf", "cover_input.conf")]

a_gui = Analysis([ENTRY], pathex=[ROOT], hiddenimports=hidden, excludes=excludes,
                 datas=DATAS, noarchive=False)
a_cli = Analysis([ENTRY], pathex=[ROOT], hiddenimports=hidden, excludes=excludes,
                 datas=DATAS, noarchive=False)
MERGE((a_gui, "HueGhost", "HueGhost"), (a_cli, "hue-ghost", "hue-ghost"))

pyz_gui = PYZ(a_gui.pure)
pyz_cli = PYZ(a_cli.pure)

exe_gui = EXE(pyz_gui, a_gui.scripts, exclude_binaries=True, name="HueGhost", console=False, icon=ICON,
              upx=False)
exe_cli = EXE(pyz_cli, a_cli.scripts, exclude_binaries=True, name="hue-ghost", console=True, icon=ICON,
              upx=False)

coll = COLLECT(exe_gui, a_gui.binaries, a_gui.datas, exe_cli, a_cli.binaries, a_cli.datas,
               name="HueGhost", upx=False)
