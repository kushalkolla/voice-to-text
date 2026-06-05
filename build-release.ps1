<#
.SYNOPSIS
    Build shareable installers for VoiceType.

.DESCRIPTION
    Produces, in .\dist:
      * VoiceType-Setup.exe  - a proper double-click installer wizard (Inno Setup):
                               welcome -> license -> installs -> finish, with a real
                               entry in "Apps & features" and an uninstaller. Free,
                               no code-signing.
      * VoiceType.zip        - the same app as a plain folder (extract, run Install.bat)

    The installer is small because it does NOT bundle Python, the libraries, or the
    speech model - it sets those up on the target PC during install (one-time
    internet, ~1 GB). It auto-detects an NVIDIA GPU and uses it; otherwise CPU.
    After setup the app runs 100% offline.

    Inno Setup is the only build-time requirement (free):
        winget install -e --id JRSoftware.InnoSetup

    Run it from this folder:
        powershell -ExecutionPolicy Bypass -File build-release.ps1

.PARAMETER ZipOnly  Build only VoiceType.zip (skip the installer .exe).
#>
[CmdletBinding()]
param([switch]$ZipOnly)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }

# Files/folders that make up a clean install (everything except local state).
$include = @(
    "voicetype",
    "Install.bat", "install.ps1", "uninstall.ps1",
    "Start Voice Typing.bat", "Open Voice Typing.bat", "open-voice-typing.ps1",
    "requirements.txt", "requirements-gpu.txt", "README.md", "LICENSE", "config.example.json"
)

$dist  = Join-Path $PSScriptRoot "dist"
$stage = Join-Path $dist "stage"
foreach ($d in @($dist, $stage)) {
    if (Test-Path $d) { Remove-Item $d -Recurse -Force }
    New-Item -ItemType Directory -Path $d -Force | Out-Null
}

# --- 1. Stage the clean app -------------------------------------------------
Step "Staging app files"
foreach ($item in $include) {
    $src = Join-Path $PSScriptRoot $item
    if (-not (Test-Path $src)) { Write-Warning "missing: $item"; continue }
    Copy-Item $src -Destination $stage -Recurse -Force
}
Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force
Ok "Staged $((Get-ChildItem $stage -Recurse -File | Measure-Object).Count) files."

# --- 2. Zip (archive root = the files themselves) ---------------------------
Step "Building VoiceType.zip"
$zip = Join-Path $dist "VoiceType.zip"
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -Force
Ok ("VoiceType.zip  ({0:N2} MB)" -f ((Get-Item $zip).Length / 1MB))

if ($ZipOnly) {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "`nDone (zip only): $zip" -ForegroundColor Green
    return
}

# --- 3. Proper installer wizard via Inno Setup ------------------------------
$iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

$exe = Join-Path $dist "VoiceType-Setup.exe"
if ($iscc) {
    Step "Building VoiceType-Setup.exe (Inno Setup wizard)"
    $iss = Join-Path $PSScriptRoot "installer\voicetype.iss"
    & $iscc /Qp "/DAppSource=$stage" "/DOutputDir=$dist" $iss
    if (Test-Path $exe) {
        Ok ("VoiceType-Setup.exe  ({0:N2} MB)" -f ((Get-Item $exe).Length / 1MB))
    } else {
        Write-Warning "Inno Setup ran but produced no .exe. The zip is still ready: $zip"
    }
} else {
    Write-Warning "Inno Setup not found - skipping the installer wizard."
    Write-Warning "Install it (free) with:  winget install -e --id JRSoftware.InnoSetup"
    Write-Warning "then re-run this script. Meanwhile the shareable zip is ready: $zip"
}

# --- 4. Tidy up -------------------------------------------------------------
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue

if (Test-Path $exe) {
    Write-Host "`nDone. Share the installer:" -ForegroundColor Green
    Write-Host "  * $exe   (double-click -> installs like a normal app)"
    Write-Host "  * $zip   (alternative: extract, run Install.bat)"
} else {
    Write-Host "`nDone. Shareable zip: $zip" -ForegroundColor Green
}
