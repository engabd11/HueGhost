# Builds two single-file executables into .\dist:
#   HueGhost.exe   - windowed; double-click = tray app (no console window)
#   hue-ghost.exe  - console; the CLI (setup / doctor / run / status ...)
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

python -m pip install --upgrade pip | Out-Null
python -m pip install -e ".[build]" | Out-Null

$common = @(
  "--noconfirm", "--onefile", "--clean",
  "--paths", ".",
  "--add-data", "hueghost\ghost_input.conf;hueghost",
  "--collect-submodules", "hueghost",
  "--hidden-import", "pystray._win32",
  "--hidden-import", "PIL.Image",
  "scripts\hueghost_entry.py"
)

python -m PyInstaller --name HueGhost --windowed @common
python -m PyInstaller --name hue-ghost --console @common

Get-ChildItem dist\*.exe | ForEach-Object { "{0,-16} {1,8:N1} MB" -f $_.Name, ($_.Length / 1MB) }
