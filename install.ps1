<#
.SYNOPSIS
    One-command setup for VoiceType on any Windows 10/11 PC.

.DESCRIPTION
    Installs Python (via winget if missing), creates an isolated virtual
    environment, installs the free/offline dependencies, optionally installs a
    Java runtime for grammar correction, pre-downloads the speech model, and
    creates Start Menu / Desktop / startup shortcuts.

    Nothing here costs money and nothing phones home. Run it from this folder:
        powershell -ExecutionPolicy Bypass -File install.ps1

.PARAMETER SkipModel    Don't pre-download the speech model (downloads on first use instead).
.PARAMETER NoJava       Don't auto-install Java (grammar correction stays off until Java exists).
.PARAMETER StartOnLogin Add VoiceType to Windows startup so it runs at login.
.PARAMETER Desktop      Also create a Desktop shortcut.
.PARAMETER Model        Override the speech model to download (e.g. base.en, medium.en).
#>
[CmdletBinding()]
param(
    [switch]$SkipModel,
    [switch]$NoJava,
    [switch]$StartOnLogin,
    [switch]$Desktop,
    [switch]$Gpu,       # force-install the GPU libraries even if no NVIDIA GPU is detected
    [switch]$NoGpu,     # never install the GPU libraries (stay on CPU)
    [string]$Model
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Write-Step($msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

Write-Host "VoiceType installer" -ForegroundColor White
Write-Host "Free, offline talk-to-type. No accounts, no API keys, no cost.`n" -ForegroundColor DarkGray

# --- 1. Find or install Python ------------------------------------------------
function Test-PyVersion($exe) {
    try {
        $v = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $v) { return $false }
        $parts = $v.Trim().Split('.')
        return ([int]$parts[0] -gt 3) -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 9)
    } catch { return $false }
}

function Find-Python {
    # Prefer the py launcher's newest 3.x, then a real python.exe on PATH,
    # then common install locations. Skip the Windows Store alias stub.
    $cands = New-Object System.Collections.Generic.List[string]
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $exe = (& py -3 -c "import sys; print(sys.executable)" 2>$null)
            if ($LASTEXITCODE -eq 0 -and $exe) { $cands.Add($exe.Trim()) }
        } catch {}
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and $python.Source -notlike "*WindowsApps*") { $cands.Add($python.Source) }
    $cands.Add("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
    $cands.Add("$env:LOCALAPPDATA\Programs\Python\Python313\python.exe")
    $cands.Add("$env:LOCALAPPDATA\Programs\Python\Python311\python.exe")
    foreach ($c in $cands) {
        if ($c -and (Test-Path $c) -and (Test-PyVersion $c)) { return $c }
    }
    return $null
}

Write-Step "Looking for Python 3.9+"
$python = Find-Python
if (-not $python) {
    Write-Warn2 "Python not found. Installing Python 3.12 via winget..."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is unavailable. Please install Python 3.12 from https://python.org and re-run."
    }
    winget install -e --id Python.Python.3.12 --silent `
        --accept-package-agreements --accept-source-agreements | Out-Null
    $python = Find-Python
    if (-not $python) {
        throw "Python was installed but could not be located. Open a new terminal and re-run install.ps1."
    }
}
Write-Ok "Using Python: $python"

# --- 2. Virtual environment + dependencies -----------------------------------
Write-Step "Creating virtual environment (.venv)"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & $python -m venv .venv
}
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
Write-Ok "Virtual environment ready."

Write-Step "Installing dependencies (this can take a few minutes the first time)"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r requirements.txt
Write-Ok "Dependencies installed."

# --- 2b. GPU acceleration (auto-detected) ------------------------------------
# One installer, every machine: if there's an NVIDIA GPU we add the CUDA
# libraries for ~10-20x faster dictation; otherwise we stay lean on CPU. The
# app auto-detects at runtime and always falls back to CPU if anything's off.
function Test-NvidiaGpu {
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { return $true }
    try {
        return [bool]((Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue) |
            Where-Object { $_.Name -match 'NVIDIA' })
    } catch { return $false }
}

Write-Step "Checking for an NVIDIA GPU (optional speed boost)"
if ($NoGpu) {
    Write-Warn2 "Skipping GPU libraries (-NoGpu). Running on CPU."
} elseif ($Gpu -or (Test-NvidiaGpu)) {
    Write-Ok "NVIDIA GPU found - installing CUDA libraries (~1.2 GB) for much faster dictation."
    try {
        & $venvPy -m pip install -r requirements-gpu.txt
        Write-Ok "GPU acceleration enabled."
    } catch {
        Write-Warn2 "GPU library install failed; the app will run on CPU (still fully works)."
    }
} else {
    Write-Ok "No NVIDIA GPU detected - using CPU (no extra download). Everything still works."
}

# --- 3. Java for grammar correction (optional) -------------------------------
Write-Step "Checking Java (for local grammar correction)"
$java = Get-Command java -ErrorAction SilentlyContinue
if ($java) {
    Write-Ok "Java found - grammar correction enabled."
} elseif ($NoJava) {
    Write-Warn2 "Skipping Java. Grammar correction will stay off until Java is installed."
} else {
    Write-Warn2 "Java not found. Installing Microsoft OpenJDK 17 via winget..."
    try {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            winget install -e --id Microsoft.OpenJDK.17 --silent `
                --accept-package-agreements --accept-source-agreements | Out-Null
            Write-Ok "Java installed."
        } else {
            Write-Warn2 "winget unavailable; grammar correction stays off (everything else works)."
        }
    } catch {
        Write-Warn2 "Could not auto-install Java; grammar correction stays off (everything else works)."
    }
}

# --- 4. Pre-download the speech model ----------------------------------------
if (-not $SkipModel) {
    Write-Step "Downloading the speech model (one-time, ~200-500 MB)"
    if ($Model) { $env:VOICETYPE_MODEL = $Model }  # not required, but handy for advanced users
    try {
        & $venvPy -m voicetype --download
        Write-Ok "Model ready."
    } catch {
        Write-Warn2 "Model pre-download failed; it will download automatically on first use."
    }
} else {
    Write-Warn2 "Skipping model pre-download (will download on first dictation)."
}

# --- 5. Shortcuts ------------------------------------------------------------
function New-Shortcut($lnkPath, $target, $arguments, $workdir, $icon) {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnkPath)
    $sc.TargetPath = $target
    $sc.Arguments = $arguments
    $sc.WorkingDirectory = $workdir
    if ($icon) { $sc.IconLocation = $icon } else { $sc.IconLocation = "$target,0" }
    $sc.Description = "VoiceType - free offline talk-to-type"
    $sc.Save()
}

Write-Step "Creating shortcuts"
$pythonw = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
$iconFile = Join-Path $PSScriptRoot "voicetype\assets\voicetype.ico"
$icon = if (Test-Path $iconFile) { "$iconFile,0" } else { $null }
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\VoiceType.lnk"
New-Shortcut $startMenu $pythonw "-m voicetype" $PSScriptRoot $icon
Write-Ok "Start Menu shortcut created."

if ($Desktop) {
    $desktopLnk = Join-Path ([Environment]::GetFolderPath('Desktop')) "VoiceType.lnk"
    New-Shortcut $desktopLnk $pythonw "-m voicetype" $PSScriptRoot $icon
    Write-Ok "Desktop shortcut created."
}
if ($StartOnLogin) {
    $startupLnk = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\VoiceType.lnk"
    New-Shortcut $startupLnk $pythonw "-m voicetype" $PSScriptRoot $icon
    Write-Ok "VoiceType will start automatically at login."
}

# --- Done --------------------------------------------------------------------
Write-Host "`nAll set!" -ForegroundColor Green
Write-Host "Start it from the Start Menu (VoiceType) or by double-clicking 'Start Voice Typing.bat'."
Write-Host "Then click into any text box and press  Left Ctrl + Left Alt  to start talking."
Write-Host "Your words type in live as you speak; click the X on the pill (or Esc) to stop."
Write-Host "Right-click the tray icon to change settings or quit.`n"
