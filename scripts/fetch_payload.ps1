# Collects what the installer bundles besides the app itself:
#   installer\payload\mpv\   mpv.exe (+ dlls)      from a local mpv install, or the latest shinchiro build
#   installer\payload\vdd\   signed Virtual Display Driver + devcon + our vdd_settings.xml
param(
  [string]$MpvFrom = "",
  [string]$VddVersion = "25.7.23"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$payload = Join-Path $root "installer\payload"
New-Item -ItemType Directory -Force "$payload\mpv", "$payload\vdd" | Out-Null

# ---- mpv --------------------------------------------------------------------------------
if (-not (Test-Path "$payload\mpv\mpv.exe")) {
  $candidates = @(@($MpvFrom, "C:\Program Files\MPV Player\mpv.exe", "C:\Program Files\mpv\mpv.exe") | Where-Object { $_ -and (Test-Path $_) })
  if ($candidates.Count -gt 0) {
    $src = Split-Path -Parent $candidates[0]
    Write-Host "mpv: copying from $src"
    Copy-Item "$src\mpv.exe" "$payload\mpv\" -Force
    Get-ChildItem "$src\*.dll" -ErrorAction SilentlyContinue | Copy-Item -Destination "$payload\mpv\" -Force
  } else {
    Write-Host "mpv: downloading the latest shinchiro build"
    $hdr = @{}
    if ($env:GITHUB_TOKEN) { $hdr["Authorization"] = "Bearer $env:GITHUB_TOKEN" }   # CI: avoid the anonymous rate limit
    $rel = Invoke-RestMethod "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest" -Headers $hdr
    $asset = $rel.assets | Where-Object { $_.name -match '^mpv-x86_64-\d{8}-git-[0-9a-f]+\.7z$' } | Select-Object -First 1
    if (-not $asset) { $asset = $rel.assets | Where-Object { $_.name -match '^mpv-x86_64.*\.7z$' -and $_.name -notmatch 'v3|dev' } | Select-Object -First 1 }
    if (-not $asset) { throw ("no mpv x86_64 asset found; assets: " + (($rel.assets | ForEach-Object { $_.name }) -join ", ")) }
    Write-Host "mpv: $($asset.name)"
    $tmp = Join-Path $env:TEMP "mpv.7z"
    Invoke-WebRequest $asset.browser_download_url -OutFile $tmp
    $sz = Get-Command 7z -ErrorAction SilentlyContinue
    if (-not $sz) { $sz = Get-Command "C:\Program Files\7-Zip\7z.exe" -ErrorAction SilentlyContinue }
    if (-not $sz) { throw "7z is needed to extract mpv (choco install 7zip)" }
    & $sz.Source x $tmp "-o$payload\mpv" mpv.exe *.dll -y | Out-Null
  }
  Get-ChildItem "$payload\mpv" | ForEach-Object { "  {0,-24} {1,8:N1} MB" -f $_.Name, ($_.Length / 1MB) }
} else { Write-Host "mpv: already in payload" }

# ---- virtual display driver ----------------------------------------------------------------
if (-not (Test-Path "$payload\vdd\MttVDD.inf")) {
  $url = "https://github.com/VirtualDrivers/Virtual-Display-Driver/releases/download/$VddVersion/VDD.Control.$VddVersion.zip"
  Write-Host "vdd: downloading $url"
  $zip = Join-Path $env:TEMP "vdd.zip"
  Invoke-WebRequest $url -OutFile $zip
  $x = Join-Path $env:TEMP "vdd_x"
  if (Test-Path $x) { Remove-Item $x -Recurse -Force }
  Expand-Archive $zip $x
  Copy-Item "$x\SignedDrivers\x86\VDD\*" "$payload\vdd\" -Force
  Copy-Item "$x\Dependencies\devcon.exe" "$payload\vdd\" -Force
  Copy-Item (Join-Path $root "installer\vdd_settings.xml") "$payload\vdd\vdd_settings.xml" -Force
  $sig = Get-AuthenticodeSignature "$payload\vdd\mttvdd.cat"
  if ($sig.Status -ne "Valid") { throw "virtual display driver catalog signature is not valid: $($sig.Status)" }
  Write-Host "vdd: signed by $($sig.SignerCertificate.Subject)"
} else { Write-Host "vdd: already in payload" }
