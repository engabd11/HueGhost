# Builds dist\installer\HueGhost-Setup-<version>.exe
#   1. python deps + PyInstaller one-folder build (installer\HueGhost.spec)
#   2. payload (mpv, virtual display driver)   scripts\fetch_payload.ps1
#   3. Inno Setup compile                      installer\HueGhost.iss
param([switch]$SkipPayload, [switch]$SkipBuild)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = (python -c "import hueghost; print(hueghost.__version__)").Trim()
$env:HUEGHOST_VERSION = $version
Write-Host "== Hue Ghost $version =="

if (-not $SkipBuild) {
  python -m pip install --upgrade pip | Out-Null
  python -m pip install -e ".[build]" | Out-Null
  python scripts\make_icon.py
  python -m PyInstaller installer\HueGhost.spec --noconfirm --clean
  if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
}

if (-not $SkipPayload) { & "$PSScriptRoot\fetch_payload.ps1" }

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
Write-Host "ISCC: $iscc"
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }
& $iscc "installer\HueGhost.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
Get-ChildItem dist\installer\*.exe | ForEach-Object { "{0,-34} {1,8:N1} MB" -f $_.Name, ($_.Length / 1MB) }
