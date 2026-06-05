<#
.SYNOPSIS
    Remove VoiceType's local install (virtual environment, shortcuts, logs).

.DESCRIPTION
    By default this removes the .venv, logs, and shortcuts but leaves your
    config.json and downloaded model/grammar caches in place. Use -Purge to also
    delete the cached speech model and LanguageTool download.

    Run from this folder:
        powershell -ExecutionPolicy Bypass -File uninstall.ps1
#>
[CmdletBinding()]
param([switch]$Purge)

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

Write-Step "Removing virtual environment"
if (Test-Path ".venv") { Remove-Item ".venv" -Recurse -Force }

Write-Step "Removing logs"
if (Test-Path "logs") { Remove-Item "logs" -Recurse -Force }

Write-Step "Removing shortcuts"
$shortcuts = @(
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\VoiceType.lnk"),
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\VoiceType.lnk"),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) "VoiceType.lnk")
)
foreach ($s in $shortcuts) { if (Test-Path $s) { Remove-Item $s -Force } }

if ($Purge) {
    Write-Step "Purging cached model + grammar downloads"
    $hf = Join-Path $env:USERPROFILE ".cache\huggingface"
    if (Test-Path $hf) { Remove-Item $hf -Recurse -Force -ErrorAction SilentlyContinue }
    $lt = Join-Path $env:APPDATA "..\Local\language_tool_python"
    $lt = [System.IO.Path]::GetFullPath($lt)
    if (Test-Path $lt) { Remove-Item $lt -Recurse -Force -ErrorAction SilentlyContinue }
    $ltCache = Join-Path $env:USERPROFILE ".cache\language_tool_python"
    if (Test-Path $ltCache) { Remove-Item $ltCache -Recurse -Force -ErrorAction SilentlyContinue }
}

Write-Host "`nVoiceType has been removed." -ForegroundColor Green
Write-Host "Your config.json was kept. Delete this folder to remove everything else." -ForegroundColor DarkGray
if (-not $Purge) {
    Write-Host "Tip: re-run with -Purge to also delete the downloaded model and grammar engine." -ForegroundColor DarkGray
}
