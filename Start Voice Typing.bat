@echo off
rem Launch VoiceType with no console window. Run install.ps1 first if you
rem haven't yet. The app lives in the system tray; right-click it to quit.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m voicetype
) else (
    echo VoiceType is not installed yet.
    echo Right-click install.ps1 and choose "Run with PowerShell", or run:
    echo     powershell -ExecutionPolicy Bypass -File install.ps1
    pause
)
