@echo off
rem ============================================================
rem  VoiceType one-click installer.
rem  Just double-click this file. It installs everything (free),
rem  starts VoiceType, and makes it run automatically at login.
rem  Then click into any text box and press  Left Ctrl + Left Alt  to talk.
rem ============================================================
cd /d "%~dp0"
echo Installing VoiceType - free, offline, no accounts, no cost.
echo This can take a few minutes the first time (it downloads the speech model).
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -StartOnLogin -Desktop
if exist ".venv\Scripts\pythonw.exe" (
    echo.
    echo Starting VoiceType...
    start "" ".venv\Scripts\pythonw.exe" -m voicetype
    echo.
    echo All done!  VoiceType is in your system tray and will start with Windows.
    echo Click into any text box and press  Left Ctrl + Left Alt  to dictate.
) else (
    echo.
    echo Something went wrong during install. See the messages above.
)
echo.
pause
